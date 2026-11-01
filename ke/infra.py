"""Conservative recovery from infrastructure failures, with a budget.

Hermes' circuit breaker parks a card (``blocked`` + a ``gave_up`` event, ``block_kind`` NULL)
after repeated worker failures. When the failures were the host's and not the work's, parking
the card is a false alarm, so :class:`InfraRecovery` may undo it. The rule is deliberately
narrow, and when in doubt it does NOT undo. A block is undone only if ALL of these hold:

* the card is ``blocked`` with no ``block_kind`` (a typed block is somebody's decision, a human
  one above all) and the newest event of the card is the breaker's own ``gave_up``, not one
  of the terminal-provider / protocol-violation trips (a retry cannot heal those);
* every failed run since the last ``completed`` run has an error that matches an
  infrastructure pattern (:func:`classify_exit`): a content, quota or unknown failure vetoes it;
* nothing can be executing: no unfinished run, a ``current_run_id`` that points to a finished
  run (or is NULL), and no live worker pid on the card or on the runs involved;
* the assessment still holds when it is repeated right before the unblock (same ``gave_up``
  event, same runs): a card touched in between is left alone;
* the card's own budget is not used up (``infra_undo_count`` per ``infra_undo_window_s``).

Hermes' ``unblock_task`` resets the card's failure counter, so undoing without a limit would
neutralise the breaker for good. The budget is the safeguard: once it is used up the card
stays blocked and the owner gets ONE alert per card and window; this module never retries
past it.

Separately, ``infra_alert_count`` infrastructure exits within ``infra_alert_window_s`` raise a
systemic alert, at most once per window. Exits arrive through Hermes' ``on_kanban_worker_exited``
hook (:meth:`InfraRecovery.on_worker_exited`), which never raises into the dispatcher.

``evaluate`` only decides and acts on cards; the alerts it owes are returned as dicts and the
coordinator delivers them, calling :meth:`InfraRecovery.mark_alerted` after a send went out.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from . import hermes_api
from .leader import pid_alive
from .messages import undo_comment
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)

INFRA, QUOTA, CONTENT, UNKNOWN = "infra", "quota", "content", "unknown"

# Run outcomes that are not failures: a handoff or a deliberate block is not the work crashing.
# Any other outcome that is not "completed" counts as a failure (and an unclassifiable one vetoes).
_NOT_FAILURES = frozenset({"blocked", "review_requested", "changes_requested", "scheduled"})
# gave_up payload flags of trips a retry cannot heal, or that Hermes holds for an operator
# (``sticky``: systemic same-error bursts and force_trip): never undone, whatever the error says.
_UNHEALABLE_FLAGS = ("sticky", "terminal_provider", "protocol_violation", "protocol_violations")


def matched_pattern(error_text: Optional[str], patterns) -> Optional[str]:
    """The first of ``patterns`` found (case-insensitively) in ``error_text``, or ``None``."""
    text = str(error_text or "").lower()
    if not text.strip():
        return None
    for pattern in patterns or ():
        if pattern and str(pattern).lower() in text:
            return str(pattern)
    return None


def classify_exit(error_text: Optional[str], exit_kind: Optional[str], patterns) -> str:
    """``"infra"`` | ``"quota"`` | ``"content"`` | ``"unknown"`` for one worker exit.

    A rate-limited exit is ``quota`` (a quota wall is not the host's fault and must never be
    retried by us); an error that contains one of ``patterns`` is ``infra``; any other error
    text is ``content``; no error text at all is ``unknown``.
    """
    if exit_kind == "rate_limited":
        return QUOTA
    if matched_pattern(error_text, patterns) is not None:
        return INFRA
    if error_text is not None and str(error_text).strip():
        return CONTENT
    return UNKNOWN


def _run_class(run: Any, patterns) -> str:
    meta = run.metadata if isinstance(getattr(run, "metadata", None), dict) else {}
    kind = "rate_limited" if run.outcome == "rate_limited" else meta.get("exit_kind")
    return classify_exit(run.error, kind, patterns)


def _live_pid(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    return pid_alive(pid)


class InfraRecovery:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        api: Any = hermes_api,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.api = api
        self.clock = clock

    # -- exits (hook side) ---------------------------------------------------------
    def record_exit(self, board: str, tid: str, run_id: Any, classification: str, now: float) -> bool:
        """Log one worker exit as ``actions(kind="infra_exit")``; False if this run was already logged.

        ``detail`` is ``"<classification>:<run_id>"``; only the ``infra`` ones feed the systemic alert.
        """
        detail = f"{classification}:{run_id}"
        if run_id is not None and self.store.action_exists(board, tid, "infra_exit", detail):
            return False
        self.store.log_action(board, tid, "infra_exit", detail, int(now))
        return True

    def on_worker_exited(self, **kw: Any) -> None:
        """``on_kanban_worker_exited`` handler. Never raises into the dispatcher."""
        try:
            tid = kw.get("task_id")
            if not tid:
                return
            reported = kw.get("board") or "default"
            run_id = kw.get("run_id")
            exit_kind = kw.get("exit_kind")
            if not isinstance(exit_kind, str):
                exit_kind = None
            board = self._board_of(str(tid), reported)
            error = self._run_error(board, str(tid), run_id)
            classification = classify_exit(error, exit_kind, self.settings.infra_patterns)
            self.record_exit(board, str(tid), run_id, classification, self.clock())
        except Exception:
            log.exception("kanban-empowered: recording a worker exit failed")

    def _board_of(self, tid: str, reported: str) -> str:
        """The board that holds ``tid``: ``reported`` when the card is there, else the first board that has it.

        Hermes fills the hook's ``board`` with ``get_current_board()``, which in the gateway is not the
        board of the exited worker when that is a non-default board; task ids are unique across boards.
        ``reported`` is kept when no board has the card (or none can be read).
        """
        def has(board: str) -> bool:
            try:
                with self.api.connect(board) as conn:
                    return self.api.task(conn, tid) is not None
            except Exception:
                log.debug("kanban-empowered: could not look for %s on board %s", tid, board, exc_info=True)
                return False

        if has(reported):
            return reported
        try:
            boards = list(self.api.boards())
        except Exception:
            return reported
        for board in boards:
            if board != reported and has(board):
                return board
        return reported

    def _run_error(self, board: str, tid: str, run_id: Any) -> Optional[str]:
        """The error Hermes booked on the run that just ended (``None`` when there is none)."""
        if run_id is None:
            return None
        with self.api.connect(board) as conn:
            for run in self.api.runs(conn, tid):
                if run.id == run_id:
                    return run.error
        return None

    # -- evaluation ------------------------------------------------------------------
    def evaluate(self, now: float) -> list[dict]:
        """Undo the breaker blocks that qualify; return the alerts that are owed (see module doc)."""
        alerts: list[dict] = []
        try:
            boards = list(self.api.boards())
        except Exception:
            log.exception("kanban-empowered: infra recovery could not list the boards")
            boards = []
        for board in boards:
            try:
                self._evaluate_board(board, now, alerts)
            except Exception:
                log.exception("kanban-empowered: infra recovery failed on board %s", board)
        try:
            alert = self._systemic_alert(now)
            if alert is not None:
                alerts.append(alert)
        except Exception:
            log.exception("kanban-empowered: infra systemic check failed")
        return alerts

    def _evaluate_board(self, board: str, now: float, alerts: list[dict]) -> None:
        with self.api.connect(board) as conn:
            for task in self.api.list_tasks(conn, status="blocked"):
                try:
                    self._consider(conn, board, task, now, alerts)
                except Exception:
                    log.exception("kanban-empowered: infra recovery failed on %s/%s", board, task.id)

    def _consider(self, conn: Any, board: str, task: Any, now: float, alerts: list[dict]) -> None:
        first = self._assess(conn, task.id)
        if first is None:
            return
        s = self.settings
        used = self.store.infra_undos_since(board, task.id, now - s.infra_undo_window_s)
        if used >= s.infra_undo_count:
            alert = self._budget_alert(board, task, used, now)
            if alert is not None:
                alerts.append(alert)
            return
        # Repeat the whole assessment right before acting: the card must be exactly as judged.
        if self._assess(conn, task.id) != first:
            return
        trip_id, count, patterns = first
        # Spend the budget first: if anything below fails, the safe side is one undo too few.
        # Then count again: a second coordinator (leader handover) may have spent in between, and
        # whoever sees more undos than the budget gives its own row back and does nothing.
        row_id = self.store.add_infra_undo(board, task.id, int(now))
        if self.store.infra_undos_since(board, task.id, now - s.infra_undo_window_s) > s.infra_undo_count:
            self.store.delete_infra_undo(row_id)
            return
        if not self.api.unblock(conn, task.id):
            log.warning("kanban-empowered: could not unblock %s/%s after an infra assessment", board, task.id)
            return
        try:
            self.api.comment(conn, task.id, undo_comment(count, ", ".join(patterns)))
        except Exception:
            log.exception("kanban-empowered: could not comment on %s/%s", board, task.id)
        self.store.log_action(
            board, task.id, "infra_undo", f"{count} infrastructure failure(s): {', '.join(patterns)}", int(now)
        )

    def _assess(self, conn: Any, tid: str) -> Optional[tuple[int, int, tuple[str, ...]]]:
        """``(gave_up event id, failed runs, matched patterns)`` when ALL the undo conditions hold, else ``None``.

        Reads the card afresh each time it is called; the caller compares two assessments.
        """
        task = self.api.task(conn, tid)
        if task is None or task.status != "blocked" or task.block_kind is not None:
            return None
        events = self.api.events(conn, tid)
        if not events or events[-1].kind != "gave_up":
            if any(e.kind == "gave_up" for e in events):
                log.debug("kanban-empowered: %s is not undone: an event follows its gave_up (%s)", tid, events[-1].kind)
            return None
        trip = events[-1]
        payload = trip.payload if isinstance(trip.payload, dict) else {}
        if any(payload.get(flag) for flag in _UNHEALABLE_FLAGS):
            return None
        runs = list(self.api.runs(conn, tid))
        failed: list[Any] = []
        for run in runs:
            if run.ended_at is None:
                return None  # an unfinished run: something may be executing
            if run.outcome == "completed":
                failed = []  # only the failures after the last success count
            elif run.outcome not in _NOT_FAILURES:
                failed.append(run)
        if not failed:
            return None
        if self._execution_possible(task, runs, failed):
            return None
        patterns: list[str] = []
        for run in failed:
            if _run_class(run, self.settings.infra_patterns) != INFRA:
                return None
            found = matched_pattern(run.error, self.settings.infra_patterns)
            if found is not None and found not in patterns:
                patterns.append(found)
        return int(trip.id), len(failed), tuple(patterns)

    @staticmethod
    def _execution_possible(task: Any, runs: list, failed: list) -> bool:
        """True unless it is certain nothing of this card is running."""
        if task.current_run_id is not None:
            current = next((r for r in runs if r.id == task.current_run_id), None)
            if current is None or current.ended_at is None:
                return True  # points to nothing we can see, or to a live run: doubt
        if _live_pid(task.worker_pid):
            return True
        # a worker that outlived its own run (closed rows keep its pid) is still executing
        return any(_live_pid(run.worker_pid) for run in failed)

    # -- alerts ------------------------------------------------------------------
    def _budget_alert(self, board: str, task: Any, used: int, now: float) -> Optional[dict]:
        key = f"infra_budget_alert:{board}:{task.id}"
        if not self._alert_due(key, now, self.settings.infra_undo_window_s):
            return None
        return {
            "kind": "budget", "key": key, "board": board, "task_id": task.id,
            "title": task.title, "count": used, "window_s": self.settings.infra_undo_window_s,
        }

    def _systemic_alert(self, now: float) -> Optional[dict]:
        s = self.settings
        if s.infra_alert_count <= 0:
            return None
        n = self.store.count_actions_since("infra_exit", int(now - s.infra_alert_window_s), "infra:")
        if n < s.infra_alert_count:
            return None
        key = "infra_systemic_alert"
        if not self._alert_due(key, now, s.infra_alert_window_s):
            return None
        return {"kind": "systemic", "key": key, "n": n, "window_s": s.infra_alert_window_s}

    def _alert_due(self, key: str, now: float, window_s: float) -> bool:
        last = self.store.kv_get(key)
        if last is None:
            return True
        try:
            return now - float(last) >= window_s
        except ValueError:
            return True

    def mark_alerted(self, alert: dict, now: float) -> None:
        """Record that ``alert`` was delivered: no new one for the same key within its window."""
        self.store.kv_set(alert["key"], repr(float(now)))
