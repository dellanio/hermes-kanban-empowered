"""Human wait tracker: owner, deadlines and condensed reminders for cards blocked on a person.

A card's *current block* is its newest ``blocked`` or ``gave_up`` event (:func:`current_block`):
its ``version`` is that event's id and its start time the event's ``created_at``, so every new
block (a re-block, or a park by Hermes' failure breaker) opens a new version and a reminder
answered for an old block can never act on the new one.

A *wait* is a blocked card whose current block is a ``blocked`` event with a human kind (one of
``Settings.human_kinds`` or none): :func:`human_block` is the single definition, shared by the
tracker, ``conditions.fleet_status`` and the panel. A breaker park (newest event ``gave_up``) is
NOT a human wait, whatever ``block_kind`` the card still carries from an earlier block (Hermes
keeps it across an unblock): infrastructure recovery and its alerts handle it, and Hermes' own
notifier announces the ``gave_up``. Once somebody classifies the park as a human block (Hermes
appends a ``blocked`` event), it becomes a wait counted from that event.

Deadlines, counted from the start of the wait: one reminder per ``Settings.reminders_s``
entry (30 min, 2 h), the escalation at ``escalate_after_s`` (8 h), then a digest every
``digest_every_s`` (daily; ``blocking_digest_s`` for a wait that holds other cards back). The
first digest is due ``digest`` after the escalation, each later one ``digest`` after the previous
digest was sent. :meth:`WaitTracker.collect_due` is pure with respect to the
sent state: it reports the highest stage already due and not yet sent, at most one per
wait, and only :meth:`WaitTracker.mark_sent` advances it. After a gateway outage every
overdue stage therefore collapses into a single message, and a wait first seen when it is
already old gets one message at once (its highest stage) and the digest cadence from then on.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from . import hermes_api
from .asks import Ask, parse_ask
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Due:
    board: str
    task_id: str
    version: int
    owner: str
    stage: str  # "reminder1" | "reminder2" | "escalation" | "digest" (reminderN for extra reminders)
    age_s: int
    ask: Optional[Ask] = None
    held_behind: list = field(default_factory=list)
    reason: str = ""  # raw block reason; what the message shows when ``ask`` is None
    board_name: str = ""  # the board's display name ("" -> the slug is shown)
    title: str = ""  # the card title: the action shown when there is no ask and no human reason
    body: str = ""  # the card body: its first sentence is the context line of such an item


def _payload(event: Any) -> dict:
    p = getattr(event, "payload", None)
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return {}
    return p if isinstance(p, dict) else {}


BLOCK_EVENTS = frozenset({"blocked", "gave_up"})


@dataclass(frozen=True)
class Block:
    """A card's current block: the newest ``blocked`` / ``gave_up`` event."""

    version: int  # the event id
    started_at: int  # the event's created_at
    kind: Optional[str]  # the block kind of a ``blocked`` event; None for a breaker park
    breaker: bool  # True when the newest event is the failure breaker's ``gave_up``
    reason: str  # the block reason, or the breaker's error


def _latest_block_event(events: list) -> Any:
    found = [e for e in events if e.kind in BLOCK_EVENTS]
    return max(found, key=lambda e: e.id) if found else None


def block_version(events: list) -> int:
    """Version of a card's current block: its newest ``blocked``/``gave_up`` event id, 0 when none."""
    ev = _latest_block_event(events)
    return int(ev.id) if ev is not None else 0


def current_block(task: Any, events: list) -> Optional[Block]:
    """The card's current block (see the module doc), or ``None`` when it never was blocked."""
    ev = _latest_block_event(events)
    if ev is None:
        return None
    payload = _payload(ev)
    if ev.kind == "gave_up":
        reason = payload.get("error") or getattr(task, "last_failure_error", None) or ""
        return Block(int(ev.id), int(ev.created_at), None, True, reason if isinstance(reason, str) else "")
    kind = payload.get("kind")
    reason = payload.get("reason")
    return Block(
        int(ev.id), int(ev.created_at), kind if isinstance(kind, str) else None, False,
        reason if isinstance(reason, str) else "",
    )


def human_block(task: Any, events: list, settings: Settings) -> Optional[Block]:
    """The block ``task`` waits on a person for, or ``None``: THE definition of a human wait.

    The card is ``blocked`` and its current block is a ``blocked`` event whose kind is human
    (in ``settings.human_kinds``, or none). Breaker parks and cards with no block event are not.
    """
    if task is None or task.status != "blocked":
        return None
    block = current_block(task, events)
    if block is None or block.breaker:
        return None
    if block.kind is not None and block.kind not in settings.human_kinds:
        return None
    return block


def display_name(api: Any, board: str) -> str:
    """The board's display name from Hermes' board metadata, ``""`` when it cannot be read."""
    try:
        fn = getattr(api, "board_name", None)
        return str(fn(board) or "") if fn is not None else ""
    except Exception:
        return ""


def held_behind(api: Any, conn: Any, tid: str) -> list[str]:
    """``todo`` descendants of ``tid``, breadth first, each once (only ``todo`` cards are walked)."""
    held: list[str] = []
    seen = {tid}
    queue = deque([tid])
    while queue:
        for child in api.children(conn, queue.popleft()):
            if child in seen:
                continue
            seen.add(child)
            task = api.task(conn, child)
            if task is not None and task.status == "todo":
                held.append(child)
                queue.append(child)
    return held


class WaitTracker:
    def __init__(self, settings: Settings, store: Store, api: Any = hermes_api) -> None:
        self.settings = settings
        self.store = store
        self.api = api

    # -- schedule ------------------------------------------------------------
    def _fixed_stages(self) -> list[tuple[int, str]]:
        """``(offset, stage)`` in time order for the non-repeating stages."""
        s = self.settings
        stages = [(off, f"reminder{i}") for i, off in enumerate(sorted(s.reminders_s), start=1)]
        stages.append((s.escalate_after_s, "escalation"))
        stages.sort(key=lambda p: p[0])  # stable: an escalation tied with a reminder stays last
        return stages

    def _highest_due_stage(self, age: int, digest: Optional[int] = None) -> Optional[tuple[str, int, int]]:
        """``(stage, offset, rank)`` of the latest deadline at or before ``age``, or ``None``."""
        fixed = self._fixed_stages()
        s = self.settings
        digest = max(1, s.digest_every_s if digest is None else digest)
        if age >= s.escalate_after_s + digest:
            k = (age - s.escalate_after_s) // digest
            return "digest", s.escalate_after_s + k * digest, len(fixed)
        found = None
        for rank, (off, name) in enumerate(fixed):
            if off <= age:
                found = (name, off, rank)
        return found

    def _rank(self, stage: Optional[str]) -> int:
        """Position of an already-sent stage in the schedule (-1 for none or an unknown name)."""
        if stage is None:
            return -1
        fixed = self._fixed_stages()
        if stage == "digest":
            return len(fixed)
        for rank, (_, name) in enumerate(fixed):
            if name == stage:
                return rank
        return -1

    def _digest_period(self, row: dict, age: int) -> int:
        """Digest period of a wait: ``blocking_digest_s`` when it holds other cards back, else ``digest_every_s``.

        The cards held are only looked up when the two periods differ and a digest could be due.
        """
        s = self.settings
        plain, blocking = max(1, s.digest_every_s), max(1, s.blocking_digest_s)
        if plain == blocking or age < s.escalate_after_s + min(plain, blocking):
            return plain
        try:
            with self.api.connect(row["board"]) as conn:
                return blocking if held_behind(self.api, conn, row["task_id"]) else plain
        except Exception:
            log.exception(
                "kanban-empowered: could not count the cards held by %s/%s", row["board"], row["task_id"]
            )
            return plain

    # -- sync ----------------------------------------------------------------
    def sync(self, now: float) -> None:
        """Mirror the human-blocked cards into ``waits`` and drop rows that no longer apply.

        A board that cannot be read keeps its rows untouched (a locked DB must not erase
        reminder state); rows of boards that no longer exist are removed.
        """
        boards = list(self.api.boards())
        wanted: set[tuple[str, str, int]] = set()
        readable: set[str] = set()
        for board in boards:
            try:
                for tid, version, started_at in self._human_blocks(board):
                    self.store.upsert_wait(board, tid, version, self.settings.owner_for(board), started_at)
                    wanted.add((board, tid, version))
                readable.add(board)
            except Exception:
                log.exception("kanban-empowered: could not read human waits of board %s", board)
        known = set(boards)
        for row in self.store.all_waits():
            key = (row["board"], row["task_id"], row["version"])
            if key in wanted:
                continue
            if row["board"] in readable or row["board"] not in known:
                self.store.delete_wait(*key)

    def _human_blocks(self, board: str) -> list[tuple[str, int, int]]:
        found: list[tuple[str, int, int]] = []
        with self.api.connect(board) as conn:
            for task in self.api.list_tasks(conn, status="blocked"):
                block = human_block(task, self.api.events(conn, task.id), self.settings)
                if block is not None:
                    found.append((task.id, block.version, block.started_at))
        return found

    # -- due -----------------------------------------------------------------
    def collect_due(self, now: float) -> list[Due]:
        """At most one :class:`Due` per wait: the highest overdue stage not yet sent."""
        out: list[Due] = []
        for row in self.store.all_waits():
            try:
                age = max(0, int(now - row["started_at"]))
                digest = self._digest_period(row, age)
                hit = self._highest_due_stage(age, digest)
                if hit is None:
                    continue
                stage, offset, rank = hit
                sent = self._rank(row["stage"])
                if sent > rank:
                    continue
                if sent == rank:
                    # only a digest repeats, one period after the previous one was sent
                    last = row["last_sent_at"]
                    if stage != "digest" or (last is not None and now - last < digest):
                        continue
                due = self._build(row, stage, age)
                if due is not None:
                    out.append(due)
            except Exception:
                log.exception(
                    "kanban-empowered: could not evaluate wait %s/%s", row["board"], row["task_id"]
                )
        return out

    def due_for(self, row: dict, now: float) -> Optional[Due]:
        """A :class:`Due` for ``row`` as it stands now (its current stage), for a re-send in the current
        message format; ``None`` when the card is not that human wait any more."""
        age = max(0, int(now - row["started_at"]))
        return self._build(row, row["stage"] or "reminder1", age)

    def _build(self, row: dict, stage: str, age: int) -> Optional[Due]:
        board, tid, version = row["board"], row["task_id"], row["version"]
        with self.api.connect(board) as conn:
            task = self.api.task(conn, tid)
            if task is None or task.status != "blocked":
                return None  # resolved since the last sync; the next sync removes the row
            block = human_block(task, self.api.events(conn, tid), self.settings)
            if block is None or block.version != version:
                return None  # re-blocked since the last sync: the new version is not tracked yet
            reason = block.reason
            title = str(getattr(task, "title", "") or "")
            body = str(getattr(task, "body", "") or "")
            held = held_behind(self.api, conn, tid)
        return Due(
            board=board,
            task_id=tid,
            version=version,
            owner=row["owner"] or self.settings.owner_for(board),
            stage=stage,
            age_s=age,
            ask=parse_ask(reason),
            held_behind=held,
            reason=reason,
            board_name=display_name(self.api, board),
            title=title,
            body=body,
        )

    # -- sent ----------------------------------------------------------------
    def mark_sent(self, due: Due, now: float) -> None:
        self.store.mark_wait_sent(due.board, due.task_id, due.version, due.stage, int(now))
