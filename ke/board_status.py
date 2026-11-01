"""Board (project) status: active / suspended / done.

Hermes boards have no lifecycle status, so the plugin keeps one in its own Store (``board_status``,
see ``Store.set_board_status``); ``board.json`` is never written. A board without a row is ``active``.

A non-active board (suspended or done):

* gets no reminders, digests or escalations for its waits (``WaitTracker``), and reactivating it restarts
  the ladder from the reactivation time (``waits.ladder_anchor``);
* is left out of the pinned live panel (one "N suspended projects" line instead) and of the dashboard
  metrics; its items go to a separate "suspended projects" section, still answerable;
* has its new claims held by the core veto hook ``kanban_pre_claim`` (:func:`make_pre_claim_hook`), in any
  profile's process. Running workers are not interrupted.

The hook runs inside Hermes' ``claim_task`` write transaction, on a helper thread bounded by
``kanban.veto_hook_timeout_seconds``. It therefore reads the shared Store through its own short-lived
read-only connection with a small busy timeout (:class:`StatusReader`), caches the statuses for at most
``CACHE_TTL_S`` and returns ``None`` (proceed) on any error: fail-open, like every core hook here.

Which board is the card on? Not the hook's ``board`` argument: Hermes fills it with its *current* board
(``get_current_board()``: the context override, ``HERMES_KANBAN_BOARD`` or the ``current`` file), and the
gateway dispatcher claims every board's cards without pinning that board as current. So while every board
is active the hook answers at once without touching any board; otherwise it looks the card up on each
live suspended/done board (the named board first) through ``hermes_api.task_exists`` (a plain read; the
board being claimed is in WAL mode, so the claim's open write transaction does not block it). Card ids are
random per card but only unique per board, so a hit must also be a ``ready`` card with the claimed
card's assignee (``hermes_api.claimable_task``) before it counts. A board this process cannot read as
itself (a pinned ``HERMES_KANBAN_DB`` makes every slug resolve to the pinned file) is never searched, so a
pinned worker or operator gateway cannot wrongly refuse; the pinned board itself is still checked. Nor
is a board this process never opened: the claiming process always has the claimed card's board open (it
holds a connection to it), and opening another one here would run Hermes' first-time init (integrity
check, schema, migrations, under its process-wide init lock) inside the claim's write transaction.

Cost. The scan runs under the claimed board's write lock and a refused card stays ``ready``, so every
dispatcher tick asks again. Therefore: the live boards are listed once per scan; the answer is cached per
card id (a hit until the statuses change, since cards never move between boards; a miss for
``NEGATIVE_TTL_S``; at most ``LOOKUP_CACHE_MAX`` ids); and the whole scan has a time budget,
``SCAN_BUDGET_S``, well under the core's 3 s cap. When the budget runs out the hook fails open (the claim
proceeds), nothing is cached, and a warning is logged once per process. A board that cannot be read is
skipped (the others are still checked); a miss with such a board is not cached. A single lookup cannot be
interrupted, so the budget is checked between boards; the core's cap still bounds a lookup that hangs.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from . import hermes_api
from .messages import t
from .store import BOARD_STATUSES

log = logging.getLogger(__name__)

CACHE_TTL_S = 5.0  # a status change reaches every claimer within this long
BUSY_MS = 250  # how long a read waits on a locked Store before giving up (fail-open)
SCAN_BUDGET_S = 1.0  # the whole board scan of one claim; the core abandons the callback at 3 s
NEGATIVE_TTL_S = 30.0  # "this card is on no non-active board" is trusted this long
LOOKUP_CACHE_MAX = 2048  # card ids remembered by the hook

_budget_warned = False  # the scan-budget warning is logged once per process

_ALIASES = {
    "active": "active", "ativo": "active", "ativa": "active",
    "suspended": "suspended", "suspenso": "suspended", "suspensa": "suspended",
    "done": "done", "concluido": "done", "concluida": "done",
}


def normalize(value: Any) -> Optional[str]:
    """The internal status for an English or Portuguese name (any case, accents optional), else ``None``."""
    if not isinstance(value, str):
        return None
    folded = unicodedata.normalize("NFKD", value.strip().lower())
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _ALIASES.get(plain)


def label(status: str) -> str:
    """The status as the user reads it (Ativo / Suspenso / Concluído in pt; Active / Suspended / Done in en)."""
    return t(f"board_status.label.{status}") if status in BOARD_STATUSES else status


def refuse_reason(status: str) -> Optional[str]:
    """The ``kanban_pre_claim`` refusal text for a non-active status, ``None`` for ``active``."""
    if status == "suspended":
        return t("board_status.refuse.suspended")
    if status == "done":
        return t("board_status.refuse.done")
    return None


class StatusReader:
    """Read-only, cached view of the ``board_status`` table of the shared Store (any profile's process).

    Never creates or writes the file: a missing file or a Store not migrated yet means every board is
    active. Read failures raise (the caller decides; the hook fails open) and are never cached.
    """

    def __init__(
        self, db_path: Path, ttl_s: float = CACHE_TTL_S, busy_ms: int = BUSY_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(db_path)
        self.ttl_s = float(ttl_s)
        self.busy_ms = int(busy_ms)
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[dict[str, str]] = None
        self._cached_at = 0.0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True,
            timeout=self.busy_ms / 1000.0, check_same_thread=False,
        )
        conn.execute(f"PRAGMA busy_timeout = {self.busy_ms}")
        return conn

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            rows = conn.execute("SELECT board, status FROM board_status").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}  # a Store written before the status existed: nothing is suspended
            raise
        finally:
            conn.close()
        return {str(b): str(s) for b, s in rows}

    def statuses(self) -> dict[str, str]:
        """``{board: status}`` of every board with a recorded status (cached for ``ttl_s``)."""
        now = self.clock()
        with self._lock:
            if self._cached is not None and now - self._cached_at < self.ttl_s:
                return self._cached
        fresh = self._read()
        with self._lock:
            self._cached, self._cached_at = fresh, now
        return fresh

    def status(self, board: str) -> str:
        return self.statuses().get(board, "active")


class _LookupCache:
    """``task_id -> (board or None, expires_at or None)``, bounded LRU, dropped whenever the set of non-active
    boards changes (a hit is then no longer known to matter, a miss may have become a hit)."""

    def __init__(self, max_size: int = LOOKUP_CACHE_MAX) -> None:
        self.max_size = max_size
        self._lock = threading.Lock()
        self._key: Optional[frozenset] = None
        self._items: "OrderedDict[str, tuple[Optional[str], Optional[float]]]" = OrderedDict()

    def get(self, key: frozenset, task_id: str, now: float) -> tuple[bool, Optional[str]]:
        with self._lock:
            if key != self._key:
                self._key, self._items = key, OrderedDict()
                return False, None
            hit = self._items.get(task_id)
            if hit is None:
                return False, None
            board, expires = hit
            if expires is not None and now >= expires:
                del self._items[task_id]
                return False, None
            self._items.move_to_end(task_id)
            return True, board

    def put(self, key: frozenset, task_id: str, board: Optional[str], expires: Optional[float]) -> None:
        with self._lock:
            if key != self._key:
                self._key, self._items = key, OrderedDict()
            self._items[task_id] = (board, expires)
            self._items.move_to_end(task_id)
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)


def _warn_budget(task_id: Any, scanned: int, total: int) -> None:
    global _budget_warned
    if _budget_warned:
        return
    _budget_warned = True
    log.warning(
        "kanban-empowered: the board status check ran out of its %.1f s budget after %d of %d suspended/done "
        "boards (card %s); claims proceed unchecked when this happens (logged once per process)",
        SCAN_BUDGET_S, scanned, total, task_id,
    )


def make_pre_claim_hook(
    reader: Any,
    locate: Optional[Callable[..., bool]] = None,
    boards: Optional[Callable[[], Any]] = None,
    clock: Callable[[], float] = time.monotonic,
    budget_s: Optional[float] = None,
) -> Callable[..., Optional[dict]]:
    """The ``kanban_pre_claim`` callback: ``{"refuse": reason}`` for a card of a suspended/done board, else ``None``.

    Hermes calls it with ``task_id, board, assignee, workspace_path, claimer`` (+ ``telemetry_schema_version``).
    ``board`` is only a hint (see the module doc). ``boards()`` (default ``hermes_api.boards``) lists the live
    boards once per scan; ``locate(board, task_id, assignee)`` (default ``hermes_api.claimable_task`` against
    that list) tells whether the card being claimed is on that board. Answers are cached (see the module doc) and the scan is bounded
    by ``budget_s`` (default ``SCAN_BUDGET_S``). Any error returns ``None``: fail-open.
    """
    lister = boards if boards is not None else hermes_api.boards
    cache = _LookupCache()

    def kanban_pre_claim(
        task_id: Any = None, board: Any = None, assignee: Any = None, **_kw: Any
    ) -> Optional[dict]:
        try:
            if not task_id:
                return None
            tid = str(task_id)
            who = None if assignee is None else str(assignee)
            ckey = f"{tid}\x00{who or ''}"
            off = {b: s for b, s in reader.statuses().items() if s != "active"}
            if not off:
                return None  # the common case: nothing to look up
            key = frozenset(off.items())
            now = clock()
            known, where = cache.get(key, ckey, now)
            if not known:
                where = _scan(tid, who, board, off, now)
                if where is _UNKNOWN:
                    return None  # out of budget, or a board could not be read: fail-open, not cached
                cache.put(key, ckey, where, None if where else now + NEGATIVE_TTL_S)
            if where is None:
                return None
            reason = refuse_reason(off[where])
            return {"refuse": reason} if reason else None
        except Exception as exc:
            log.debug("kanban-empowered: board status check failed for %r (fail-open): %s", task_id, exc)
            return None

    def _scan(tid: str, who: Optional[str], hint: Any, off: dict, started: float) -> Any:
        live = list(lister())
        live_set = set(live)
        find = locate if locate is not None else (
            lambda b, t, a: hermes_api.claimable_task(b, t, a, live=live_set)
        )
        candidates = [b for b in sorted(off, key=lambda b: (b != hint, b)) if b in live_set]
        budget = SCAN_BUDGET_S if budget_s is None else budget_s
        failed = False
        for n, candidate in enumerate(candidates):
            if clock() - started >= budget:
                _warn_budget(tid, n, len(candidates))
                return _UNKNOWN
            try:
                if find(candidate, tid, who):
                    return candidate
            except Exception as exc:  # one unreadable board must not let the other boards' cards through
                failed = True
                log.debug("kanban-empowered: could not look card %s up on board %s: %s", tid, candidate, exc)
        return _UNKNOWN if failed else None

    return kanban_pre_claim


_UNKNOWN = object()  # the scan could not decide (budget spent, or a board failed and nothing was found)
