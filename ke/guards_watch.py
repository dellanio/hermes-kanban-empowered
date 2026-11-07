"""GuardWatch (alert when a card stays under a respawn guard) and the honest ``release`` command.

Hermes' dispatcher reports, on every tick of a board, which ready/review cards it refused to
respawn (``DispatchResult.respawn_guarded``: ``(task_id, reason)``). The coordinator feeds each of
those observations to :meth:`GuardWatch.observe`; a card that stays guarded for the same reason
for ``guard_alert_after_s`` (30 min) is due an alert, repeated every ``digest_every_s`` while the
guard lasts (one message is easy to miss, and the card never moves on its own).

Counting absences. ``observe`` is called once per tick of a board, and ``evaluate`` is throttled
to 30 s across all boards, so absence is counted per *observation* of that board (one tick of
the board with the card missing from its ``respawn_guarded``), never per evaluation and never by
another board's ticks. A tracked card missing from three consecutive observations of its board
is dropped, because a single missing tick proves nothing (a per-profile cap makes the dispatcher
skip a card before it reaches the guard). A card that is gone or no longer waiting to be
dispatched (``ready``/``review``) is dropped at its first absence. The counters live in memory:
after a restart a stale row is dropped by the status check at its first absence.

``release`` is the one way out the plugin offers for a guard, and it only claims success when it
really handed the card to another profile.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from . import conditions, hermes_api
from .messages import t
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

ABSENT_CYCLES = 3  # consecutive observations of a board without the card before it is forgotten
_WAITING = frozenset({"ready", "review"})  # statuses the dispatcher may still respawn from
RELEASABLE = frozenset({"active_pr", "blocker_auth"})
_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)


def _pr_url(body: Any) -> Optional[str]:
    match = _PR_URL_RE.search(str(body or ""))
    return match.group(0) if match else None


class GuardWatch:
    def __init__(self, settings: Settings, store: Store, api: Any = hermes_api) -> None:
        self.settings = settings
        self.store = store
        self.api = api
        self._absent: dict[tuple[str, str], int] = {}

    # -- feeding ---------------------------------------------------------------
    def observe(self, board: str, guarded: list[tuple[str, str]], now: float) -> None:
        """One observation of ``board``: upsert what is guarded now, age out what is not."""
        seen: dict[str, str] = {}
        for task_id, reason in guarded or ():
            seen[str(task_id)] = str(reason)
        for task_id, reason in seen.items():
            self.store.upsert_guard_alert(board, task_id, reason, int(now))
            self._absent.pop((board, task_id), None)
        absent = [r["task_id"] for r in self.store.guard_alerts(board) if r["task_id"] not in seen]
        if not absent:
            return
        waiting = self._waiting(board, absent)  # None: the board could not be read
        for task_id in absent:
            key = (board, task_id)
            if waiting is not None and task_id not in waiting:
                self._forget(board, task_id)
                continue
            self._absent[key] = self._absent.get(key, 0) + 1
            if self._absent[key] >= ABSENT_CYCLES:
                self._forget(board, task_id)

    def _forget(self, board: str, task_id: str) -> None:
        self.store.delete_guard_alert(board, task_id)
        self._absent.pop((board, task_id), None)

    def _waiting(self, board: str, task_ids: list[str]) -> Optional[set[str]]:
        """The subset of ``task_ids`` still waiting to be dispatched, or ``None`` if unreadable."""
        try:
            with self.api.connect(board) as conn:
                out = set()
                for task_id in task_ids:
                    task = self.api.task(conn, task_id)
                    if task is not None and task.status in _WAITING:
                        out.add(task_id)
                return out
        except Exception as exc:
            log.warning("kanban-empowered: cannot check the guarded cards of board %r: %s", board, exc)
            return None

    # -- alerts ----------------------------------------------------------------
    def due_alerts(self, now: float) -> list[dict]:
        """Rows guarded for at least ``guard_alert_after_s`` and not alerted in the last ``digest_every_s``
        (``age_s`` added)."""
        rows = self.store.due_guard_alerts(
            now - self.settings.guard_alert_after_s, now - self.settings.digest_every_s
        )
        return [{**row, "age_s": max(0, int(now - row["first_seen"]))} for row in rows]

    def mark_alerted(self, alert: dict, now: float) -> bool:
        """Record that ``alert`` was delivered (no-op if its reason changed meanwhile)."""
        return self.store.mark_guard_alerted(alert["board"], alert["task_id"], alert["reason"], int(now))

    def enrich(self, alerts: list[dict], now: float) -> list[dict]:
        """Add title, assignee and (for ``active_pr``) the newest PR link to each alert.

        Alerts whose card vanished or left the dispatch queue are dropped and forgotten. A board
        that cannot be read keeps its alerts, without the details.
        """
        out: list[dict] = []
        by_board: dict[str, list[dict]] = {}
        for alert in alerts:
            by_board.setdefault(alert["board"], []).append(alert)
        for board, group in by_board.items():
            try:
                with self.api.connect(board) as conn:
                    for alert in group:
                        task = self.api.task(conn, alert["task_id"])
                        if task is None or task.status not in _WAITING:
                            self._forget(board, alert["task_id"])
                            continue
                        item = {**alert, "title": task.title, "assignee": task.assignee}
                        if alert["reason"] == "active_pr":
                            self._add_pr(conn, alert["task_id"], item)
                        out.append(item)
            except Exception as exc:
                log.warning("kanban-empowered: cannot read board %r for a guard alert: %s", board, exc)
                done = {a["task_id"] for a in out if a["board"] == board}
                out.extend(a for a in group if a["task_id"] not in done)
        return out

    def _add_pr(self, conn: Any, task_id: str, item: dict) -> None:
        newest = None
        for comment in self.api.comments(conn, task_id):
            url = _pr_url(comment.body)
            if url and (newest is None or (comment.created_at, comment.id) >= newest[0]):
                newest = ((comment.created_at, comment.id), url)
        if newest is not None:
            item["pr_url"] = newest[1]
            item["pr_at"] = int(newest[0][0])


# -- release -----------------------------------------------------------------------
def release_result(
    conn: Any,
    board: str,
    tid: str,
    to_profile: str,
    api: Any = hermes_api,
    store: Optional[Store] = None,
    now: float = 0,
    actor: str = "",
) -> tuple[bool, str]:
    """``(released, message)``: hand ``tid`` to ``to_profile`` when its guard allows it.

    Only a card still waiting to be dispatched whose current guard (``conditions.current_guard``)
    is ``active_pr`` or ``blocker_auth`` can be released, and only to an existing profile other
    than its current assignee. Anything else changes nothing and says why.
    """
    task = api.task(conn, tid)
    if task is None:
        return False, t("explain.unknown_card", card=tid, board=board)
    guard = conditions.current_guard(conn, tid) if task.status in _WAITING else None
    reason = guard["reason"] if guard else None
    if reason not in RELEASABLE:
        return False, t("release.no_exit", reason=reason or t("release.no_guard"))
    if to_profile not in api.profiles():
        return False, t("release.no_profile", to=to_profile)
    if to_profile == task.assignee:
        return False, t("release.same_profile")
    try:
        assigned = api.assign(conn, tid, to_profile)
    except RuntimeError:  # the card got claimed in the meantime
        return False, t("release.running")
    if not assigned:
        return False, t("explain.unknown_card", card=tid, board=board)
    previous = task.assignee or t("guards.no_assignee")
    try:
        api.comment(
            conn, tid,
            t("release.comment", reason=reason, to=to_profile, previous=previous, actor=actor or "cli"),
        )
    except Exception:
        log.exception("kanban-empowered: could not comment on %s/%s", board, tid)
    if store is not None:
        try:
            store.log_action(board, tid, "guard_release", f"{actor or 'cli'}: {reason} -> {to_profile}", int(now))
        except Exception:
            log.exception("kanban-empowered: could not log guard_release for %s/%s", board, tid)
    return True, t("release.done", to=to_profile)


def release(
    conn: Any,
    board: str,
    tid: str,
    to_profile: str,
    api: Any = hermes_api,
    store: Optional[Store] = None,
    now: float = 0,
    actor: str = "",
) -> str:
    """The user-facing result string of :func:`release_result`."""
    return release_result(conn, board, tid, to_profile, api, store=store, now=now, actor=actor)[1]
