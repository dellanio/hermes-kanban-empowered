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
        if self.store.kv_get("veto_hooks_warned"):
            return
        if self.api.veto_hooks_available():
            return
        self.store.kv_set("veto_hooks_warned", "1")
        # The Telegram notice is added in Task 8; for now only the log carries it.
        log.warning(
            "kanban-empowered: this Hermes build lacks the kanban veto hooks "
            "(kanban_pre_claim / kanban_pre_complete); guards run in cooperative-only mode"
        )
