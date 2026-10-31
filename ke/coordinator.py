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
MAX_MESSAGES_PER_PASS = 3  # Telegram messages per recipient per evaluation (tick time is bounded)
VETO_RETRY_S = 600  # back-off between failed attempts to send the veto-hooks warning

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
        self._tg_down = False
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

    def _tg_send(self, chat_id: int, text: str) -> int:
        """``telegram.send`` that remembers a transport failure: Telegram is down for this evaluation."""
        try:
            return self.telegram.send(chat_id, text)
        except Exception as exc:
            if getattr(exc, "status", None) is None:  # no HTTP answer: network, DNS, timeout
                self._tg_down = True
            raise

    def _deliver_waits(self, now: float) -> None:
        """Send ``pending_dues`` grouped by owner; ``mark_sent`` only for what was actually sent.

        Whatever could not be sent stays in ``pending_dues`` (and unmarked, so the next
        evaluation offers it again). The pass is bounded: at most ``MAX_MESSAGES_PER_PASS``
        messages per recipient, and a transport failure ends it for this evaluation.

        An escalation also goes to ``escalate_to``, independently of the owner's delivery:
        two kv markers per ``(board, card, version)`` record "owner got it" and "copy sent",
        each retried until it succeeds, and the wait is marked sent only when both are done
        (so the owner is never messaged twice for the same escalation).
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

        def copy_needed(d: Due) -> bool:
            return target is not None and target != d.owner and d.stage == "escalation"

        def marker(kind: str, d: Due) -> str:
            return f"esc_{kind}:{d.board}:{d.task_id}:{d.version}"

        def owner_got_it(d: Due) -> None:
            if copy_needed(d):
                self.store.kv_set(marker("owner", d), "1")

        def copy_sent(d: Due) -> None:
            self.store.kv_set(marker("copy", d), "1")

        owner_sent: set[int] = set()  # id(due): the owner has the message (now or on an earlier pass)
        counts: dict[str, int] = {}
        try:
            by_owner: dict[str, list[Due]] = {}
            for due in dues:
                if copy_needed(due) and self.store.kv_get(marker("owner", due)):
                    owner_sent.add(id(due))  # escalation already delivered to the owner earlier
                else:
                    by_owner.setdefault(due.owner, []).append(due)
            for owner, group in by_owner.items():
                if self._tg_down:
                    break
                # urgent: nobody else hears about an escalation (no escalate_to, or the owner is it)
                urgent = target is None or target == owner
                sent = self._send_group(
                    owner, owner, group, now, counts, urgent=urgent, on_sent=owner_got_it
                )
                owner_sent.update(id(d) for d in sent)
            if target is not None:  # the copy does not depend on the owner's delivery
                by_origin: dict[str, list[Due]] = {}
                for due in dues:
                    if copy_needed(due) and not self.store.kv_get(marker("copy", due)):
                        by_origin.setdefault(due.owner, []).append(due)
                for origin, group in by_origin.items():
                    if self._tg_down:
                        break
                    self._send_group(
                        target, origin, group, now, counts, urgent=False,
                        lead=t("waits.copy_of", owner=origin), on_sent=copy_sent,
                    )
        except Exception:
            log.exception("kanban-empowered: delivering reminders failed")
        finally:
            remaining: list[Due] = []
            for due in dues:
                try:
                    done = id(due) in owner_sent and not (
                        copy_needed(due) and not self.store.kv_get(marker("copy", due))
                    )
                    if done:
                        self.waits.mark_sent(due, now)
                        continue
                except Exception:
                    log.exception("kanban-empowered: could not mark a reminder as sent")
                remaining.append(due)
            self.pending_dues = remaining

    def _send_group(
        self,
        recipient: str,
        label: str,
        group: list[Due],
        now: float,
        counts: dict[str, int],
        urgent: bool,
        lead: str = "",
        on_sent: Optional[Callable[[Due], Any]] = None,
    ) -> list[Due]:
        """Send ``group`` to ``recipient`` split to the Telegram limit; returns the dues sent.

        ``counts`` is the per-pass message tally per recipient (capped at MAX_MESSAGES_PER_PASS).
        ``on_sent`` runs for each due right after its message went out; marking waits as sent
        is left to the caller.
        """
        chat_id = self.settings.owner_ids.get(recipient)
        if chat_id is None:
            self._warn_once("no_chat:" + recipient, "kanban-empowered: no Telegram chat id for owner %r", recipient)
            return []
        sent: list[Due] = []
        for text, items in render_waits_chunks(label, group, urgent_escalation=urgent, lead=lead):
            if self._tg_down or counts.get(recipient, 0) >= MAX_MESSAGES_PER_PASS:
                break  # the rest goes out on a later evaluation
            counts[recipient] = counts.get(recipient, 0) + 1
            try:
                message_id = self._tg_send(chat_id, text)
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
            for due in items:
                if on_sent is not None:
                    try:
                        on_sent(due)
                    except Exception:
                        log.exception("kanban-empowered: could not record a delivery marker")
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
            self._tg_down = False  # a transport failure ends all Telegram sends of this evaluation
            self._warn_missing_veto_hooks(now)
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

    def _warn_missing_veto_hooks(self, now: float) -> None:
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
        self._send_veto_warning(now)

    def _send_veto_warning(self, now: float) -> None:
        """Tell the default owner once over Telegram.

        Retried on later evaluations until it lands, but after a failed attempt at most once
        every ``VETO_RETRY_S``.
        """
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
        last = self.store.kv_get("veto_hooks_warn_try")
        try:
            if last is not None and now - float(last) < VETO_RETRY_S:
                return
        except ValueError:
            pass
        try:
            self._tg_send(chat_id, t("veto.warning"))
        except Exception as exc:
            log.warning("kanban-empowered: could not send the veto-hooks warning: %s", exc)
            self.store.kv_set("veto_hooks_warn_try", repr(float(now)))
            return
        self.store.kv_set("veto_hooks_warn_sent", "1")
