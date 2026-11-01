"""Coordinator: the one place that periodically evaluates waits, guards, infra and the panel.

It is driven by Hermes' ``on_kanban_dispatch_tick`` hook. Only the process whose
active profile is ``settings.coordinator_profile`` AND that holds the
:class:`~ke.leader.LeaderLock` evaluates, and at most once per
``EVAL_INTERVAL_S`` no matter how many boards tick.

Later tasks plug their components in with :meth:`register_step`; a step is a
plain ``callable(now)`` and runs in the fixed order of ``STEP_ORDER``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Optional

from . import hermes_api, panel
from . import messages as msgs
from .guards_watch import GuardWatch
from .infra import InfraRecovery
from .messages import branded, render_config_alert, render_guard_chunks, render_infra_alert, render_waits_chunks
from .leader import LeaderLock
from .settings import Settings
from .store import Store
from .waits import Due, WaitTracker

log = logging.getLogger(__name__)

EVAL_INTERVAL_S = 30
STEP_ORDER = ("waits", "guards", "infra", "panel")
MAX_MESSAGES_PER_PASS = 3  # Telegram messages per recipient per evaluation (tick time is bounded)
TEMPLATE_KEY = "template_version"  # kv: the reminder layout version (messages.MESSAGE_TEMPLATE_VERSION) in use
VETO_RETRY_S = 600  # back-off between failed attempts to send a setup alert (veto hooks, escalation contact)
COPY_RETRY_S = 600  # back-off between failed attempts to send one escalation copy
TERMINAL_HTTP = (400, 403)  # chat not found, bot blocked: retrying cannot help
# After a transport failure (no HTTP answer: network, DNS, timeout) no message is attempted for this
# long, across evaluations: each attempt can block a dispatcher tick for the client's whole timeout.
TG_BACKOFF_S = 180

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
        # Infrastructure-failure recovery: exits are recorded by the worker-exited hook, the infra
        # step undoes the qualifying breaker blocks and sends the alerts that are owed.
        self.infra = InfraRecovery(settings, store, api, clock=clock)
        self._warned: set[str] = set()  # log-once keys (per process)
        self._tg_down = False
        self._merged_ids: set[int] = set()  # id() of due reminders already sent inside the layout re-send
        self._tg_retry_at = 0.0  # no Telegram attempt before this time (transport back-off)
        self._now = 0.0  # time of the evaluation in progress
        self.register_step("waits", self._waits_step)
        self.register_step("guards", self._guards_step)
        self.register_step("infra", self._infra_step)
        self.register_step("panel", self.update_panel)

    # -- wiring --------------------------------------------------------------
    def register_step(self, name: str, fn: Step) -> None:
        """Plug a component into one of ``STEP_ORDER`` (replaces any previous one)."""
        if name not in self.steps:
            raise ValueError(f"unknown step {name!r}; expected one of {STEP_ORDER}")
        self.steps[name] = fn

    def _waits_step(self, now: float) -> None:
        self.waits.sync(now)
        self.pending_dues = self.waits.collect_due(now)
        counts: dict[str, int] = {}  # messages per recipient this pass, shared by the re-send and the reminders
        self._merged_ids = set()
        self._resend_template(now, counts)
        self._deliver_waits(now, counts)
        self._merged_ids = set()
        self._check_escalation_setup(now)

    # -- reminder layout version -----------------------------------------------------
    @staticmethod
    def _template_marker(board: str, task_id: str, version: int, skip: bool = False) -> str:
        return f"{'tmpl-skip' if skip else 'tmpl'}:{msgs.MESSAGE_TEMPLATE_VERSION}:{board}:{task_id}:{version}"

    def _resend_template(self, now: float, counts: dict[str, int]) -> None:
        """After a change of the reminder layout, send each open wait once more in the current one.

        A wait already messaged (``stage`` set) was last shown in the layout of its time: the owner may
        have missed the news (or only ever seen an old-format message). The re-send is grouped per owner,
        recorded as ``sent_messages`` (so replies resolve) and does NOT touch ``waits.stage`` /
        ``last_sent_at``: it is not a stage. When the same owner also has reminders due in this pass,
        those go in the same message (``self._merged_ids``; ``_deliver_waits`` then only advances
        their stage). One kv marker per wait and layout version makes it idempotent across passes; a wait
        that cannot be re-sent for good (its owner has no chat id, or it cannot be rebuilt) gets a
        "skipped" marker instead. ``kv template_version`` is written once every wait is delivered or
        skipped, so a failed send is retried and a delivered one never repeated; the markers are then
        deleted.
        """
        current = str(msgs.MESSAGE_TEMPLATE_VERSION)
        if self.store.kv_get(TEMPLATE_KEY) == current:
            return
        due_now = {(d.board, d.task_id, d.version): d for d in self.pending_dues}
        todo = []
        for row in self.waits.active_rows():  # a suspended/done board's waits are not re-sent
            key = (row["board"], row["task_id"], row["version"])
            if (
                row["stage"] is None or key in due_now
                or self.store.kv_get(self._template_marker(*key))
                or self.store.kv_get(self._template_marker(*key, skip=True))
            ):
                continue
            todo.append(row)
        if todo:
            if self._tg_down or not self._telegram_ready():
                return
            by_owner: dict[str, list] = {}
            for row in todo:
                key = (row["board"], row["task_id"], row["version"])
                try:
                    due = self.waits.due_for(row, now)
                except Exception:
                    # one broken wait must not hold back the others: log it once and skip it for good
                    log.exception(
                        "kanban-empowered: could not rebuild wait %s/%s for the re-send; skipping it",
                        row["board"], row["task_id"],
                    )
                    self.store.kv_set(self._template_marker(*key, skip=True), "1")
                    continue
                if due is None:
                    continue  # resolved or re-blocked since the sync: nothing to re-send
                if self.settings.owner_ids.get(due.owner) is None:
                    # an owner without a chat id cannot be messaged at all (logged by _owner_chat): not retried
                    self._owner_chat(due.owner)
                    self.store.kv_set(self._template_marker(*key, skip=True), "1")
                    continue
                by_owner.setdefault(due.owner, []).append(due)
            resend_ids = {id(d) for group in by_owner.values() for d in group}
            now_by_owner: dict[str, list] = {}
            for d in self.pending_dues:
                if d.owner in by_owner:
                    now_by_owner.setdefault(d.owner, []).append(d)

            def delivered(d: Due) -> None:
                if id(d) in resend_ids:
                    self.store.kv_set(self._template_marker(d.board, d.task_id, d.version), "1")
                else:
                    self._merged_ids.add(id(d))  # a reminder due now that rode along: no second message

            for owner, group in by_owner.items():
                if self._tg_down:
                    break
                # the re-send items first, then the reminders due now: one message per owner, not two
                self._send_group(owner, owner, group + now_by_owner.get(owner, []), now, counts, on_sent=delivered)
            if any(
                not self.store.kv_get(self._template_marker(d.board, d.task_id, d.version))
                for group in by_owner.values() for d in group
            ):
                return
        self.store.kv_set(TEMPLATE_KEY, current)
        for prefix in ("tmpl:", "tmpl-skip:"):
            try:
                self.store.kv_delete_prefix(prefix)
            except Exception:
                log.exception("kanban-empowered: could not clean up the layout re-send markers")

    def _check_escalation_setup(self, now: float) -> None:
        """A wait reached the escalation stage but there is nobody to escalate to: one setup alert.

        The owner still gets the reminder; this tells them (once, in a message of its own) why nobody
        else was reached. A configured contact without a chat id has its own alert (``_check_setup``).
        """
        if self.settings.escalate_to is not None or self.store.kv_get("setup_escalation_no_target_sent"):
            return
        after = self.settings.escalate_after_s
        if not any(now - row["anchor"] >= after for row in self.waits.active_rows()):
            return
        self._send_setup_alert(
            "escalation_no_target", now,
            sent_key="setup_escalation_no_target_sent", try_key="setup_escalation_no_target_try",
        )

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

    # -- infrastructure recovery ---------------------------------------------------
    def _infra_step(self, now: float) -> None:
        """Run the recovery, then tell the owner about the budgets that ran out and a systemic burst.

        A card's budget alert goes to its board's owner, the systemic one to the default owner.
        Like the guard alerts: marked only after the message went out (a failure is retried on
        the next evaluation), at most ``MAX_MESSAGES_PER_PASS`` per recipient, a transport
        failure ends the pass. Recorded as kind "infra" (the reply handler ignores it).
        """
        alerts = self.infra.evaluate(now)
        if not alerts or not self._telegram_ready():
            return
        by_owner: dict[str, list[dict]] = {}
        for alert in alerts:
            owner = (
                self.settings.owner_for(alert["board"]) if alert["kind"] == "budget" else self.settings.default_owner
            )
            by_owner.setdefault(owner, []).append(alert)
        counts: dict[str, int] = {}
        for owner, group in by_owner.items():
            if self._tg_down:
                break
            chat_id = self._owner_chat(owner)
            if chat_id is None:
                continue
            for alert in group:
                if self._tg_down or counts.get(owner, 0) >= MAX_MESSAGES_PER_PASS:
                    break
                counts[owner] = counts.get(owner, 0) + 1
                try:
                    message_id = self._tg_send(chat_id, render_infra_alert(alert))
                except Exception as exc:
                    self._log_send_failure(owner, chat_id, exc)
                    if self._send_ends_pass(exc):
                        break
                    continue
                items = [{"board": alert["board"], "task_id": alert["task_id"]}] if alert["kind"] == "budget" else []
                self._record_sent(message_id, chat_id, "infra", items, now)
                try:
                    self.infra.mark_alerted(alert, now)
                except Exception:
                    log.exception("kanban-empowered: could not mark an infra alert as sent")

    # -- live panel (pinned message) ---------------------------------------------------
    def update_panel(self, now: float) -> None:
        """Keep the pinned panel message in sync with a fresh snapshot (default owner's chat).

        First time: ``send`` + ``pin``, remembering ``kv panel_message_id`` (and its chat). Afterwards
        ``edit`` only when the text changed (``kv panel_hash``). The message is recreated (send + pin,
        id overwritten) when Telegram says it is gone or cannot be edited (also when the owner's chat
        changed); any other error is logged and the id kept, so the next evaluation retries.
        At most one new message per call, so the per-pass message cap is never in play; a transport
        failure marks Telegram down for the rest of the evaluation. The numbered items are recorded
        as ``sent_messages(kind="panel")`` on every send and edit so ``"1 done"`` works on the panel.
        """
        s = self.settings
        if not s.pin_live_panel or self._tg_down or not self._telegram_ready():
            return
        owner = s.default_owner
        chat_id = self._owner_chat(owner)
        if chat_id is None:
            return
        snap = panel.build_snapshot(self.store, self.api, now, settings=s)
        text = panel.render_telegram(snap)
        items = panel.panel_items(snap)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        message_id = self._panel_message_id(chat_id)
        if message_id is not None:
            if self.store.kv_get("panel_hash") == digest:
                self._panel_sync_items(chat_id, message_id, items, now)
                self._ensure_pinned(owner, chat_id, message_id)
                return
            try:
                self._tg_call("edit", chat_id, message_id, text)
            except Exception as exc:
                reason = str(getattr(exc, "description", "") or exc).lower()
                if "message is not modified" in reason:
                    pass  # Telegram already shows exactly this text
                elif "message to edit not found" in reason or "message can't be edited" in reason:
                    message_id = None  # gone: fall through to a new message
                else:
                    status = getattr(exc, "status", None)
                    if status is None or status >= 500:
                        # Telegram may have applied the edit before the answer was lost: replies
                        # must stay checked against both the old and the attempted list
                        self._panel_record(chat_id, message_id, items, now, certain=False)
                    self._log_send_failure(owner, chat_id, exc)
                    return
            if message_id is not None:
                if self._panel_record(chat_id, message_id, items, now):
                    self.store.kv_set("panel_hash", digest)  # only once the items are recorded
                self._ensure_pinned(owner, chat_id, message_id)
                return
        try:
            message_id = self._tg_send(chat_id, text)
        except Exception as exc:
            self._log_send_failure(owner, chat_id, exc)
            return
        self.store.kv_set("panel_message_id", str(message_id))
        self.store.kv_set("panel_chat_id", str(chat_id))
        self.store.kv_set("panel_hash", "")
        self.store.kv_set("panel_pin_done", "")
        if self._panel_record(chat_id, message_id, items, now):
            self.store.kv_set("panel_hash", digest)
        self._ensure_pinned(owner, chat_id, message_id)

    def _panel_message_id(self, chat_id: int) -> Optional[int]:
        """The tracked panel message id, or ``None`` (none yet, unreadable, or sent to another chat)."""
        raw, chat = self.store.kv_get("panel_message_id"), self.store.kv_get("panel_chat_id")
        try:
            if raw and chat is not None and int(chat) == int(chat_id):
                return int(raw)
        except ValueError:
            pass
        return None

    def _panel_record(
        self, chat_id: int, message_id: int, items: list, now: float, certain: bool = True
    ) -> bool:
        """Record the numbered items of the panel message; False (logged) when that failed."""
        try:
            panel.record_items(self.store, chat_id, message_id, items, now, certain=certain)
            return True
        except Exception:
            log.exception("kanban-empowered: could not record the panel items of message %s", message_id)
            return False

    def _panel_sync_items(self, chat_id: int, message_id: int, items: list, now: float) -> None:
        """Same text, but maybe other versions: replies are validated against what is current."""
        try:
            sent = self.store.get_sent_message(chat_id, message_id)
            if sent is None or sent.get("kind") != "panel" or sent.get("items") != items:
                panel.record_items(self.store, chat_id, message_id, items, now)
        except Exception:
            log.exception("kanban-empowered: could not refresh the panel items of message %s", message_id)

    def _ensure_pinned(self, owner: str, chat_id: int, message_id: int) -> None:
        """Pin the panel once. A transient failure is retried on a later evaluation; a rejection
        (e.g. the bot may not pin here) is logged once and not retried."""
        if self.store.kv_get("panel_pin_done") == "1" or self._tg_down:
            return
        try:
            self._tg_call("pin", chat_id, message_id)
        except Exception as exc:
            self._log_send_failure(owner, chat_id, exc)
            status = getattr(exc, "status", None)
            if status is not None and 400 <= status < 500 and status != 429:
                self.store.kv_set("panel_pin_done", "1")
            return
        self.store.kv_set("panel_pin_done", "1")

    def _tg_call(self, method: str, *args: Any) -> Any:
        """``telegram.edit`` / ``telegram.pin`` that remembers a transport failure (like ``_tg_send``)."""
        if method == "edit":  # (chat_id, message_id, text): the brand line is on every edit too
            args = (*args[:2], branded(args[2]), *args[3:])
        try:
            return getattr(self.telegram, method)(*args)
        except Exception as exc:
            if getattr(exc, "status", None) is None:
                self._transport_failed()
            raise

    def _transport_failed(self) -> None:
        """No HTTP answer: Telegram is down for this evaluation and the next ``TG_BACKOFF_S``."""
        self._tg_down = True
        self._tg_retry_at = self._now + TG_BACKOFF_S

    # -- Telegram delivery of the reminders -------------------------------------
    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def _tg_send(self, chat_id: int, text: str) -> int:
        """``telegram.send`` that remembers a transport failure: Telegram is down for this evaluation."""
        try:
            return self.telegram.send(chat_id, branded(text))  # idempotent: chunks arrive branded
        except Exception as exc:
            if getattr(exc, "status", None) is None:  # no HTTP answer: network, DNS, timeout
                self._transport_failed()
            raise

    def _telegram_ready(self) -> bool:
        """False (with a once-only warning) when this process has no Bot API client."""
        if self.telegram is None:
            self._warn_once(
                "no_telegram",
                "kanban-empowered: Hermes resolved no TELEGRAM_BOT_TOKEN for the coordinator profile; "
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

    def _deliver_waits(self, now: float, counts: Optional[dict[str, int]] = None) -> None:
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
            # logged here; the owner is told once, in a separate setup alert (see _check_setup)
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
        counts = {} if counts is None else counts
        try:
            by_owner: dict[str, list[Due]] = {}
            for due in dues:
                if id(due) in self._merged_ids:
                    owner_sent.add(id(due))  # went out inside the layout re-send of this pass
                    owner_got_it(due)
                elif copy_needed(due) and self.store.kv_get(marker("owner", due)):
                    owner_sent.add(id(due))  # escalation already delivered to the owner earlier
                else:
                    by_owner.setdefault(due.owner, []).append(due)
            for owner, group in by_owner.items():
                if self._tg_down:
                    break
                sent = self._send_group(owner, owner, group, now, counts, on_sent=owner_got_it)
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
                        target, origin, group, now, counts,
                        on_sent=copy_sent, on_failed=copy_failed, copy=True,
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
                        self._template_current(due)
                        continue
                except Exception:
                    log.exception("kanban-empowered: could not mark a reminder as sent")
                remaining.append(due)
            self.pending_dues = remaining

    def _template_current(self, due: Due) -> None:
        """A reminder just sent is in the current layout: a pending layout re-send must not repeat it."""
        try:
            if self.store.kv_get(TEMPLATE_KEY) != str(msgs.MESSAGE_TEMPLATE_VERSION):
                self.store.kv_set(self._template_marker(due.board, due.task_id, due.version), "1")
        except Exception:
            log.exception("kanban-empowered: could not record the layout version of a reminder")

    def _send_group(
        self,
        recipient: str,
        label: str,
        group: list[Due],
        now: float,
        counts: dict[str, int],
        on_sent: Optional[Callable[[Due], Any]] = None,
        on_failed: Optional[Callable[[list[Due], Exception, bool], Any]] = None,
        copy: bool = False,
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
        chunks = render_waits_chunks(label, group, copy=copy)
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
            # a transport failure ends all Telegram sends of this evaluation and of the next
            # TG_BACKOFF_S (whatever is not sent stays due and goes out once the back-off is over)
            self._now = now
            self._tg_down = now < self._tg_retry_at
            self._warn_missing_veto_hooks(now)
        except Exception:
            log.exception("kanban-empowered: veto-hooks check failed")
        try:
            self._check_setup(now)
        except Exception:
            log.exception("kanban-empowered: setup check failed")
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
        """Tell the default owner once over Telegram, in a separate setup alert."""
        self._send_setup_alert("veto", now, sent_key="veto_hooks_warn_sent", try_key="veto_hooks_warn_try")

    def _check_setup(self, now: float) -> None:
        """Setup alerts other than the veto hooks: an ``escalate_to`` contact without a chat id.

        (An owner without a chat id cannot be messaged at all: that is only logged.)
        """
        target = self.settings.escalate_to
        if target is None or target in self.settings.owner_ids:
            return
        self._warn_once("no_chat:" + target, "kanban-empowered: no Telegram chat id for escalate_to %r", target)
        self._send_setup_alert(
            "escalation_no_chat", now, sent_key="setup_escalation_no_chat_sent", try_key="setup_escalation_no_chat_try"
        )

    def _send_setup_alert(self, kind: str, now: float, sent_key: str, try_key: str) -> None:
        """Send the setup alert ``kind`` to the default owner once (its own message, never inside a
        reminder). Retried on later evaluations until it lands, but after a failed attempt at most
        once every ``VETO_RETRY_S``."""
        if self.store.kv_get(sent_key) or self._tg_down or not self._telegram_ready():
            return
        chat_id = self._owner_chat(self.settings.default_owner)
        if chat_id is None:
            return
        last = self.store.kv_get(try_key)
        try:
            if last is not None and now - float(last) < VETO_RETRY_S:
                return
        except ValueError:
            pass
        try:
            self._tg_send(chat_id, render_config_alert(kind))
        except Exception as exc:
            log.warning("kanban-empowered: could not send the %s setup alert: %s", kind, exc)
            self.store.kv_set(try_key, repr(float(now)))
            return
        self.store.kv_set(sent_key, "1")
