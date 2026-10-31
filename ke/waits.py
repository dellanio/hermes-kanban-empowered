"""Human wait tracker: owner, deadlines and condensed reminders for cards blocked on a person.

A *wait* is one blocked card whose latest ``blocked`` event has a human kind (one of
``Settings.human_kinds`` or none). Its ``version`` is that event's id and its start time
the event's ``created_at``, so re-blocking a card opens a new version and a reminder
answered for the old one can never act on the new block.

Deadlines, counted from the start of the wait: one reminder per ``Settings.reminders_s``
entry (30 min, 2 h), the escalation at ``escalate_after_s`` (8 h), then a digest every
``digest_every_s`` (daily). :meth:`WaitTracker.collect_due` is pure with respect to the
sent state: it reports the highest stage already due and not yet sent, at most one per
wait, and only :meth:`WaitTracker.mark_sent` advances it. After a gateway outage every
overdue stage therefore collapses into a single message.
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


def _payload(event: Any) -> dict:
    p = getattr(event, "payload", None)
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return {}
    return p if isinstance(p, dict) else {}


def _latest_blocked(events: list) -> Any:
    blocked = [e for e in events if e.kind == "blocked"]
    return max(blocked, key=lambda e: e.id) if blocked else None


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

    def _highest_due_stage(self, age: int) -> Optional[tuple[str, int, int]]:
        """``(stage, offset, rank)`` of the latest deadline at or before ``age``, or ``None``."""
        fixed = self._fixed_stages()
        s = self.settings
        digest = max(1, s.digest_every_s)
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
        kinds = self.settings.human_kinds
        with self.api.connect(board) as conn:
            for task in self.api.list_tasks(conn, status="blocked"):
                events = self.api.events(conn, task.id)
                ev = _latest_blocked(events)
                if ev is not None:
                    kind = _payload(ev).get("kind")
                    version, started_at = int(ev.id), int(ev.created_at)
                else:  # parked by the failure breaker: no blocked event, only the card's own kind
                    kind = getattr(task, "block_kind", None)
                    last = max(events, key=lambda e: e.id) if events else None
                    version = 0
                    started_at = int(last.created_at if last is not None else task.created_at)
                if kind is not None and kind not in kinds:
                    continue
                found.append((task.id, version, started_at))
        return found

    # -- due -----------------------------------------------------------------
    def collect_due(self, now: float) -> list[Due]:
        """At most one :class:`Due` per wait: the highest overdue stage not yet sent."""
        out: list[Due] = []
        for row in self.store.all_waits():
            try:
                age = max(0, int(now - row["started_at"]))
                hit = self._highest_due_stage(age)
                if hit is None:
                    continue
                stage, offset, rank = hit
                sent = self._rank(row["stage"])
                if sent > rank:
                    continue
                if sent == rank and not (
                    stage == "digest" and (row["last_sent_at"] or 0) < row["started_at"] + offset
                ):
                    continue
                due = self._build(row, stage, age)
                if due is not None:
                    out.append(due)
            except Exception:
                log.exception(
                    "kanban-empowered: could not evaluate wait %s/%s", row["board"], row["task_id"]
                )
        return out

    def _build(self, row: dict, stage: str, age: int) -> Optional[Due]:
        board, tid, version = row["board"], row["task_id"], row["version"]
        with self.api.connect(board) as conn:
            task = self.api.task(conn, tid)
            if task is None or task.status != "blocked":
                return None  # resolved since the last sync; the next sync removes the row
            ev = _latest_blocked(self.api.events(conn, tid))
            if (int(ev.id) if ev is not None else 0) != version:
                return None  # re-blocked since the last sync: the new version is not tracked yet
            reason = _payload(ev).get("reason") if ev is not None else None
            reason = reason if isinstance(reason, str) else ""
            held = self._held_behind(conn, tid)
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
        )

    def _held_behind(self, conn: Any, tid: str) -> list[str]:
        """``todo`` descendants of ``tid``, breadth first, each once (only ``todo`` cards are walked)."""
        held: list[str] = []
        seen = {tid}
        queue = deque([tid])
        while queue:
            for child in self.api.children(conn, queue.popleft()):
                if child in seen:
                    continue
                seen.add(child)
                task = self.api.task(conn, child)
                if task is not None and task.status == "todo":
                    held.append(child)
                    queue.append(child)
        return held

    # -- sent ----------------------------------------------------------------
    def mark_sent(self, due: Due, now: float) -> None:
        self.store.mark_wait_sent(due.board, due.task_id, due.version, due.stage, int(now))
