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
from .guards_watch import GuardWatch
from .messages import render_guard_chunks, render_waits_chunks, t
from .leader import LeaderLock
from .settings import Settings
from .store import Store
from .waits import Due, WaitTracker

log = logging.getLogger(__name__)

EVAL_INTERVAL_S = 30
STEP_ORDER = ("waits", "guards", "infra", "panel")
MAX_MESSAGES_PER_PASS = 3  # Telegram messages per recipient per evaluation (tick time is bounded)
VETO_RETRY_S = 600  # back-off between failed attempts to send the veto-hooks warning
COPY_RETRY_S = 600  # back-off between failed attempts to send one escalation copy
TERMINAL_HTTP = (400, 403)  # chat not found, bot blocked: retrying cannot help

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
        # Respawn guards: every board tick feeds the watch (see ``on_tick``); the guards step
        # sends the alerts that are due.
        self.guards = GuardWatch(settings, store, api)
        self._warned: set[str] = set()  # log-once keys (per process)
        self._tg_down = False
        self.register_step("waits", self._waits_step)
        self.register_step("guards", self._guards_step)

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

    # -- respawn guards ------------------------------------------------------------
    def _guards_step(self, now: float) -> None:
        """Alert each board's owner, in one message, about the guards that outlasted the limit.

        ``GuardWatch.observe`` already ran on every tick; here only the due, unalerted rows are
        sent. An alert is marked only after its message went out, so a failure is retried on the
        next evaluation. Bounded like the reminders: ``MAX_MESSAGES_PER_PASS`` per recipient and
        a transport failure ends the pass.
        """
        due = self.guards.due_alerts(now)
        if not due or not self._telegram_ready():
            return
        due = self.guards.enrich(due, now)
        if not due:
            return
        by_owner: dict[str, list[dict]] = {}
        for alert in due:
            by_owner.setdefault(self.settings.owner_for(alert["board"]), []).append(alert)
        counts: dict[str, int] = {}
        for owner, group in by_owner.items():
            if self._tg_down:
                break
            chat_id = self._owner_chat(owner)
            if chat_id is None:
                continue
            for text, items in render_guard_chunks(group):
                if self._tg_down or counts.get(owner, 0) >= MAX_MESSAGES_PER_PASS:
                    break
                counts[owner] = counts.get(owner, 0) + 1
                try:
                    message_id = self._tg_send(chat_id, text)
                except Exception as exc:
                    self._log_send_failure(owner, chat_id, exc)
                    if self._send_ends_pass(exc):
                        break
                    continue
                # kind "guard": the reply handler only resolves kind "waits", so "done" to this does nothing
                self._record_sent(
                    message_id, chat_id, "guard",
                    [{"board": a["board"], "task_id": a["task_id"]} for a in items], now,
                )
                for alert in items:
                    try:
                        self.guards.mark_alerted(alert, now)
                    except Exception:
                        log.exception("kanban-empowered: could not mark a guard alert as sent")

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

    def _telegram_ready(self) -> bool:
        """False (with a once-only warning) when this process has no Bot API client."""
        if self.telegram is None:
            self._warn_once(
                "no_telegram",
                "kanban-empowered: TELEGRAM_BOT_TOKEN is not set in the coordinator process; "
                "Telegram messages are disabled",
            )
            return False
        return True

    def _owner_chat(self, owner: str) -> Optional[int]:
        """The Telegram chat id of ``owner`` (a once-only warning and ``None`` when unknown)."""
        chat_id = self.settings.owner_ids.get(owner)
        if chat_id is None:
            self._warn_once("no_chat:" + owner, "kanban-empowered: no Telegram chat id for owner %r", owner)
        return chat_id

    def _log_send_failure(self, recipient: str, chat_id: int, exc: Exception, terminal: bool = False) -> None:
        """Log a failed send: a permanent rejection once per (chat, error), anything else every time.

        ``terminal`` (the caller gives up for good and logs that itself) logs nothing here.
        """
        status = getattr(exc, "status", None)
        if terminal:
            return
        if status is not None and 400 <= status < 500 and status != 429:
            # a permanent rejection repeats every pass: say it once per (chat, error)
            self._warn_once(
                f"send:{chat_id}:{status}:{exc}",
                "kanban-empowered: Telegram rejected the message to %r: %s", recipient, exc,
            )
        else:
            log.warning("kanban-empowered: Telegram send to %r failed: %s", recipient, exc)

    @staticmethod
    def _send_ends_pass(exc: Exception) -> bool:
        """403 (chat unusable), transport trouble, 429 or 5xx: the rest waits for a later pass.

        A message-level 4xx (e.g. 400) does not: the messages behind it are still tried.
        """
        status = getattr(exc, "status", None)
        return status is None or status in (403, 429) or status >= 500

    def _record_sent(self, message_id: int, chat_id: int, kind: str, items: list, now: float) -> None:
        try:
            self.store.add_sent_message(message_id, chat_id, kind, items, int(now))
        except Exception:
            log.exception("kanban-empowered: could not record sent message %s", message_id)

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
        if not self._telegram_ready():
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

        def copy_failed(items: list[Due], exc: Exception, terminal: bool) -> None:
            for d in items:
                if not terminal:  # 429 / 5xx / transport: try again, but not before COPY_RETRY_S
                    self.store.kv_set(marker("copytry", d), repr(float(now)))
                    continue
                self.store.kv_set(marker("copy", d), "gave_up")  # lets the owner's wait finish
                self.store.log_action(
                    d.board, d.task_id, "escalation_copy_failed", f"{target}: {exc}", int(now)
                )
            if terminal:
                log.warning(
                    "kanban-empowered: giving up the escalation copy to %r for %d card(s): %s",
                    target, len(items), exc,
                )

        def copy_backing_off(d: Due) -> bool:
            last = self.store.kv_get(marker("copytry", d))
            try:
                return last is not None and now - float(last) < COPY_RETRY_S
            except ValueError:
                return False

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
                    if (
                        copy_needed(due)
                        and not self.store.kv_get(marker("copy", due))
                        and not copy_backing_off(due)
                    ):
                        by_origin.setdefault(due.owner, []).append(due)
                for origin, group in by_origin.items():
                    if self._tg_down:
                        break
                    self._send_group(
                        target, origin, group, now, counts, urgent=False,
                        lead=t("waits.copy_of", owner=origin), on_sent=copy_sent, on_failed=copy_failed,
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
        on_failed: Optional[Callable[[list[Due], Exception, bool], Any]] = None,
    ) -> list[Due]:
        """Send ``group`` to ``recipient`` split to the Telegram limit; returns the dues sent.

        ``counts`` is the per-pass message tally per recipient (capped at MAX_MESSAGES_PER_PASS).
        ``on_sent`` runs for each due right after its message went out; marking waits as sent
        is left to the caller. A failed send calls ``on_failed(items, exc, terminal)`` (escalation
        copies only): ``items`` is the chunk, or for a terminal 400/403 every item not yet sent,
        and the group ends. Without ``on_failed`` (owner reminders) a 403 ends the group for the
        pass (the chat is unusable), while a 400 is message-level: the next chunk is still tried.
        Rejections are logged once per (chat, status, error).
        """
        chat_id = self._owner_chat(recipient)
        if chat_id is None:
            return []
        sent: list[Due] = []
        chunks = render_waits_chunks(label, group, urgent_escalation=urgent, lead=lead)
        for index, (text, items) in enumerate(chunks):
            if self._tg_down or counts.get(recipient, 0) >= MAX_MESSAGES_PER_PASS:
                break  # the rest goes out on a later evaluation
            counts[recipient] = counts.get(recipient, 0) + 1
            try:
                message_id = self._tg_send(chat_id, text)
            except Exception as exc:
                status = getattr(exc, "status", None)
                # only an escalation copy (on_failed) gives up for good on a 400/403
                terminal = on_failed is not None and status in TERMINAL_HTTP
                self._log_send_failure(recipient, chat_id, exc, terminal)  # terminal: on_failed logs it
                if on_failed is not None:
                    rest = [d for _, chunk_items in chunks[index:] for d in chunk_items] if terminal else items
                    try:
                        on_failed(rest, exc, terminal)
                    except Exception:
                        log.exception("kanban-empowered: could not record a delivery failure")
                if terminal or self._send_ends_pass(exc):
                    # 403 (bot blocked / chat unusable), transport trouble, rate limit: retry next pass
                    break
                continue  # a message-level 4xx (e.g. 400) must not hold back the chunks behind it
            self._record_sent(
                message_id, chat_id, "waits",
                [{"board": d.board, "task_id": d.task_id, "version": d.version} for d in items], now,
            )
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
            self._observe_guards(board, result, now)  # every tick, whatever the evaluation throttle says
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

    def _observe_guards(self, board: str, result: Any, now: float) -> None:
        guarded = getattr(result, "respawn_guarded", None)
        if guarded is None:
            return
        try:
            self.guards.observe(board, list(guarded), now)
        except Exception:
            log.exception("kanban-empowered: recording the respawn guards of board %r failed", board)

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
        if not self._telegram_ready():
            return
        chat_id = self._owner_chat(self.settings.default_owner)
        if chat_id is None:
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
