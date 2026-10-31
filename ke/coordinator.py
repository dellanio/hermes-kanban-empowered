"""Coordinator: the one place that periodically evaluates waits, guards, infra and the panel.

It is driven by Hermes' ``on_kanban_dispatch_tick`` hook. Only the process whose
active profile is ``settings.coordinator_profile`` AND that holds the
:class:`~ke.leader.LeaderLock` evaluates, and at most once per
``EVAL_INTERVAL_S`` no matter how many boards tick.

Later tasks plug their components in with :meth:`register_step`; a step is a
plain ``callable(now)`` and runs in the fixed order of ``STEP_ORDER``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from . import hermes_api
from .messages import render_waits_chunks, t
from .leader import LeaderLock
from .settings import Settings
from .store import Store
from .waits import Due, WaitTracker

log = logging.getLogger(__name__)

EVAL_INTERVAL_S = 30
STEP_ORDER = ("waits", "guards", "infra", "panel")

Step = Callable[[float], Any]


class Coordinator:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        api: Any = hermes_api,
        telegram: Any = None,
        clock: Callable[[], float] = time.time,
        leader: Optional[LeaderLock] = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.api = api
        self.telegram = telegram
        self.clock = clock
        self.leader = leader or LeaderLock(settings.shared_state_dir / "leader.lock")
        self.steps: dict[str, Optional[Step]] = {name: None for name in STEP_ORDER}
        # Human waits: each evaluation syncs the tracker and keeps the reminders that are due
        # in ``pending_dues`` for the sender; nothing is marked sent here.
        self.waits = WaitTracker(settings, store, api)
        self.pending_dues: list[Due] = []
        self._warned: set[str] = set()  # log-once keys (per process)
        self.register_step("waits", self._waits_step)

    # -- wiring --------------------------------------------------------------
    def register_step(self, name: str, fn: Step) -> None:
        """Plug a component into one of ``STEP_ORDER`` (replaces any previous one)."""
        if name not in self.steps:
            raise ValueError(f"unknown step {name!r}; expected one of {STEP_ORDER}")
        self.steps[name] = fn

    def _waits_step(self, now: float) -> None:
        self.waits.sync(now)
        self.pending_dues = self.waits.collect_due(now)
        self._deliver_waits(now)

    # -- Telegram delivery of the reminders -------------------------------------
    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def _deliver_waits(self, now: float) -> None:
        """Send ``pending_dues`` grouped by owner; ``mark_sent`` only for what was actually sent.

        Whatever could not be sent stays in ``pending_dues`` (and unmarked, so the next
        evaluation offers it again). An escalation also goes to ``escalate_to`` once the owner
        has received it; that copy is best-effort (never retried, never un-marks the owner's).
        """
        dues = list(self.pending_dues)
        if not dues:
            return
        if self.telegram is None:
            self._warn_once(
                "no_telegram",
                "kanban-empowered: TELEGRAM_BOT_TOKEN is not set in the coordinator process; "
                "Telegram messages are disabled",
            )
            return
        s = self.settings
        target = s.escalate_to
        if target is not None and target not in s.owner_ids:
            self._warn_once("no_chat:" + target, "kanban-empowered: no Telegram chat id for escalate_to %r", target)
            target = None
        by_owner: dict[str, list[Due]] = {}
        for due in dues:
            by_owner.setdefault(due.owner, []).append(due)
        delivered: set[int] = set()
        copies: list[Due] = []
        for owner, group in by_owner.items():
            try:
                # urgent = nobody else will hear about an escalation (no escalate_to, or it is the owner)
                sent = self._send_group(owner, owner, group, now, urgent=(target is None), mark=True)
            except Exception:
                log.exception("kanban-empowered: delivering reminders to %r failed", owner)
                continue
            delivered.update(id(d) for d in sent)
            if target is not None and target != owner:
                copies.extend(d for d in sent if d.stage == "escalation")
        if copies:
            try:
                self._send_group(target, target, copies, now, urgent=False, mark=False)
            except Exception:
                log.exception("kanban-empowered: escalation copy to %r failed", target)
        self.pending_dues = [d for d in dues if id(d) not in delivered]

    def _send_group(
        self, recipient: str, label: str, group: list[Due], now: float, urgent: bool, mark: bool
    ) -> list[Due]:
        """Send ``group`` to ``recipient`` split to the Telegram limit; returns the dues sent."""
        chat_id = self.settings.owner_ids.get(recipient)
        if chat_id is None:
            self._warn_once("no_chat:" + recipient, "kanban-empowered: no Telegram chat id for owner %r", recipient)
            return []
        sent: list[Due] = []
        for text, items in render_waits_chunks(label, group, urgent_escalation=urgent):
            try:
                message_id = self.telegram.send(chat_id, text)
            except Exception as exc:
                log.warning("kanban-empowered: Telegram send to %r failed: %s", recipient, exc)
                status = getattr(exc, "status", None)
                if status is None or status == 429 or status >= 500:
                    break  # transport trouble / rate limit: stop now, retry on a later evaluation
                continue  # a rejected message (4xx) must not hold back the ones behind it
            try:
                self.store.add_sent_message(
                    message_id,
                    chat_id,
                    "waits",
                    [{"board": d.board, "task_id": d.task_id, "version": d.version} for d in items],
                    int(now),
                )
            except Exception:
                log.exception("kanban-empowered: could not record sent message %s", message_id)
            if mark:
                for due in items:
                    self.waits.mark_sent(due, now)
            sent.extend(items)
        return sent

    # -- hook entry ----------------------------------------------------------
    def on_tick(
        self, board: str, result: Any = None, dry_run: bool = False, **_ignored: Any
    ) -> None:
        """``on_kanban_dispatch_tick`` handler. Never raises into the dispatcher."""
        if dry_run:
            return
        try:
            if self.api.active_profile() != self.settings.coordinator_profile:
                return
            now = self.clock()
            if not self.leader.acquire(now):  # also renews the heartbeat when we lead
                return
            last = self.store.kv_get("last_eval")
            if last is not None:
                try:
                    if now - float(last) < EVAL_INTERVAL_S:
                        return
                except ValueError:
                    pass
            self.store.kv_set("last_eval", repr(float(now)))
            self.evaluate(now)
        except Exception:
            log.exception("kanban-empowered: coordinator tick failed (board=%s)", board)

    # -- evaluation ----------------------------------------------------------
    def evaluate(self, now: float) -> None:
        try:
            self._warn_missing_veto_hooks()
        except Exception:
            log.exception("kanban-empowered: veto-hooks check failed")
        for name in STEP_ORDER:
            step = self.steps[name]
            if step is None:
                continue
            try:
                step(now)
            except Exception:
                log.exception("kanban-empowered: step %r failed", name)

    def _warn_missing_veto_hooks(self) -> None:
        warned = self.store.kv_get("veto_hooks_warned")
        if warned and self.store.kv_get("veto_hooks_warn_sent"):
            return
        if self.api.veto_hooks_available():
            return
        if not warned:
            self.store.kv_set("veto_hooks_warned", "1")
            log.warning(
                "kanban-empowered: this Hermes build lacks the kanban veto hooks "
                "(kanban_pre_claim / kanban_pre_complete); guards run in cooperative-only mode"
            )
        self._send_veto_warning()

    def _send_veto_warning(self) -> None:
        """Tell the default owner once over Telegram; retried on later evaluations until it lands."""
        if self.telegram is None:
            self._warn_once(
                "no_telegram",
                "kanban-empowered: TELEGRAM_BOT_TOKEN is not set in the coordinator process; "
                "Telegram messages are disabled",
            )
            return
        owner = self.settings.default_owner
        chat_id = self.settings.owner_ids.get(owner)
        if chat_id is None:
            self._warn_once("no_chat:" + owner, "kanban-empowered: no Telegram chat id for owner %r", owner)
            return
        try:
            self.telegram.send(chat_id, t("veto.warning"))
        except Exception as exc:
            log.warning("kanban-empowered: could not send the veto-hooks warning: %s", exc)
            return
        self.store.kv_set("veto_hooks_warn_sent", "1")
