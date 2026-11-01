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
read-only connection with a small busy timeout (:class:`StatusReader`), caches the answer for at most
``CACHE_TTL_S`` and returns ``None`` (proceed) on any error: fail-open, like every core hook here.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from .messages import t
from .store import BOARD_STATUSES

log = logging.getLogger(__name__)

CACHE_TTL_S = 5.0  # a status change reaches every claimer within this long
BUSY_MS = 250  # how long a read waits on a locked Store before giving up (fail-open)

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


def make_pre_claim_hook(reader: Any) -> Callable[..., Optional[dict]]:
    """The ``kanban_pre_claim`` callback: ``{"refuse": reason}`` for a suspended/done board, else ``None``.

    Hermes calls it with ``task_id, board, assignee, workspace_path, claimer`` (+ ``telemetry_schema_version``);
    only ``board`` matters. Any error (unreadable Store, timeout, translation) returns ``None``: fail-open.
    """

    def kanban_pre_claim(task_id: Any = None, board: Any = None, **_kw: Any) -> Optional[dict]:
        try:
            if not board:
                return None
            reason = refuse_reason(reader.status(str(board)))
            return {"refuse": reason} if reason else None
        except Exception as exc:
            log.debug("kanban-empowered: board status check failed for %r (fail-open): %s", board, exc)
            return None

    return kanban_pre_claim
