"""Authenticated two-way Telegram replies: "done" answered to a reminder unblocks the card.

Replies reach the plugin through Hermes' ``pre_gateway_dispatch`` hook, which runs BEFORE
the gateway's own authorization, so every sender must be treated as untrusted. The checks
run in this fixed order and nothing about a card is read before all of them pass:

1. the event is a direct Telegram message (not relayed, not from a bot, not synthetic)
   replying to a message this plugin sent to that same chat (``sent_messages``);
   the text must be a reply command (``[n] done|feito|liguei|ok|approved|aprovado``);
2. the sender's Telegram user id is the configured owner of the selected item
   (``settings.owner_ids[settings.owner_for(board)]``). A sender who is some other
   *configured* identity (e.g. the ``escalate_to`` contact replying to an escalated copy)
   is told who owns the request; anybody else gets no answer at all and the message
   continues to the normal gateway flow (where Hermes' own auth applies);
3. ``store.mark_update`` dedupes the update (a replay is consumed in silence);
4. the message is younger than ``settings.reply_ttl_s``.

Only then is the card read: it must still be ``blocked`` with the same block version as
the item in the message (the live latest ``blocked`` event id, and the tracked wait row
when there is one), so a "done" answered to an old reminder can never resolve a newer block.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

from .messages import render_done, t
from .waits import block_version, held_behind

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "kanban-empowered"}
REPLY_RE = re.compile(r"^\s*([0-9]{1,4})?\s*(feito|done|liguei|ok|aprovado|approved)\b", re.IGNORECASE)
_ID_RE = re.compile(r"-?[1-9][0-9]{0,19}")
_COMMENT_TEXT_MAX = 500

# Serializes "validate then unblock" across gateway threads (two replies for the same card).
_resolve_lock = threading.Lock()


def _skip() -> dict:
    return dict(SKIP)


def _canonical_id(value: Any) -> Optional[str]:
    """A Telegram id as its canonical decimal string (no sign tricks, no leading zeros), or None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.fullmatch(text) else None


def _is_telegram(platform: Any) -> bool:
    value = getattr(platform, "value", platform)
    return isinstance(value, str) and value.strip().lower() == "telegram"


def _replied_to_our_bot(event: Any, telegram: Any) -> bool:
    """False only when the raw Telegram update shows the replied-to message was authored by
    someone other than the bot that sent our reminders (a gateway on a different bot token).
    Without that information (no raw update, unknown bot id) the chat/message lookup stands."""
    bot_id = getattr(telegram, "bot_id", None)
    raw = getattr(event, "raw_message", None)
    author = getattr(getattr(getattr(raw, "reply_to_message", None), "from_user", None), "id", None)
    if bot_id is None or author is None:
        return True
    return str(author) == str(bot_id)


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _COMMENT_TEXT_MAX else text[: _COMMENT_TEXT_MAX - 1] + "…"


def _reply(telegram: Any, chat_id: int, message_id: int, text: str) -> None:
    if telegram is None:
        log.warning("kanban-empowered: TELEGRAM_BOT_TOKEN is not set; cannot answer a reply")
        return
    try:
        telegram.send(chat_id, text, reply_to=message_id)
    except Exception as exc:
        log.warning("kanban-empowered: could not answer a Telegram reply: %s", exc)


def resolve_wait(
    api: Any,
    store: Any,
    board: str,
    task_id: str,
    version: int,
    comment: str,
    now: float,
    detail: str,
) -> tuple[bool, list[str]]:
    """Unblock ``task_id`` if it is still blocked on block ``version``; ``(done, held_behind)``.

    Shared by the Telegram reply and ``hermes kanban-empowered done``. On success the card is
    unblocked, commented and a ``human_done`` action is logged; the comment and the log are
    best effort once the unblock happened (the unblock itself is the decision).
    """
    with _resolve_lock:
        with api.connect(board) as conn:
            task = api.task(conn, task_id)
            if task is None or task.status != "blocked":
                return False, []
            if block_version(api.events(conn, task_id)) != int(version):
                return False, []
            wait = store.latest_wait(board, task_id)
            if wait is not None and int(wait["version"]) != int(version):
                return False, []
            if not api.unblock(conn, task_id):  # guarded on status: lost a race with another unblock
                return False, []
            try:
                api.comment(conn, task_id, comment)
            except Exception:
                log.exception("kanban-empowered: could not comment on %s/%s", board, task_id)
            try:
                store.log_action(board, task_id, "human_done", detail, int(now))
            except Exception:
                log.exception("kanban-empowered: could not log human_done for %s/%s", board, task_id)
            try:
                held = held_behind(api, conn, task_id)
            except Exception:
                log.exception("kanban-empowered: could not list the cards behind %s/%s", board, task_id)
                held = []
    return True, held


def handle_inbound(event: Any, settings: Any, store: Any, api: Any, telegram: Any, now: float) -> Optional[dict]:
    """``{"action": "skip", ...}`` when this plugin consumed the message, ``None`` to let it through."""
    # -- 1. a direct Telegram reply to one of our messages, in that same chat ----------------
    source = getattr(event, "source", None)
    if source is None or not _is_telegram(getattr(source, "platform", None)):
        return None
    if getattr(event, "internal", False) or getattr(source, "is_bot", False):
        return None
    if getattr(source, "delivered_via_upstream_relay", False):
        return None  # our bot's own adapter delivers replies directly; never trust a relayed identity
    chat = _canonical_id(getattr(source, "chat_id", None))
    sender = _canonical_id(getattr(source, "user_id", None))
    reply_to = _canonical_id(getattr(event, "reply_to_message_id", None))
    message = _canonical_id(getattr(event, "message_id", None))
    if chat is None or sender is None or reply_to is None or message is None:
        return None
    sent = store.get_sent_message(int(chat), int(reply_to))
    if sent is None or sent.get("kind") != "waits":
        return None
    if not _replied_to_our_bot(event, telegram):
        return None  # same chat and message id, but a message of another bot (DM ids are per bot)
    items = sent.get("items")
    if not isinstance(items, list) or not items:
        return None
    match = REPLY_RE.match(getattr(event, "text", None) or "")
    if match is None:
        return None  # not a reply command: a normal message for the agent
    number = int(match.group(1)) if match.group(1) else None
    if number is not None:
        selected = items[number - 1] if 1 <= number <= len(items) else None
    else:
        selected = items[0] if len(items) == 1 else None

    # -- 2. the sender is the configured owner of the selected item -------------------------
    scope = [selected] if selected is not None else items
    owners = sorted({settings.owner_for(str(item["board"])) for item in scope})
    owner_ids = {o: _canonical_id(settings.owner_ids.get(o)) for o in owners}
    if sender not in {v for v in owner_ids.values() if v is not None}:
        known = {_canonical_id(v) for v in settings.owner_ids.values()}
        if sender not in known:
            return None  # a stranger: no answer, nothing consumed, nothing read
        if not store.mark_update(f"tg:{chat}:{message}", int(now)):
            return _skip()
        _reply(telegram, int(chat), int(message), t("inbound.not_owner", owner=", ".join(owners)))
        return _skip()

    # -- 3. each update acts at most once ---------------------------------------------------
    if not store.mark_update(f"tg:{chat}:{message}", int(now)):
        return _skip()

    # -- 4. the request is still fresh ------------------------------------------------------
    if now - int(sent["created_at"]) >= settings.reply_ttl_s:
        _reply(telegram, int(chat), int(message), t("inbound.expired"))
        return _skip()

    if selected is None:
        _reply(telegram, int(chat), int(message), t("inbound.which_item", n=len(items)))
        return _skip()

    board, task_id, version = str(selected["board"]), str(selected["task_id"]), int(selected["version"])
    name = getattr(source, "user_name", None) or getattr(event, "user_name", None) or sender
    comment = t("inbound.comment.telegram", name=_clip(name), user_id=sender, text=_clip(event.text))
    try:
        done, held = resolve_wait(api, store, board, task_id, version, comment, now, f"telegram:{sender}")
    except Exception:
        log.exception("kanban-empowered: resolving %s/%s from Telegram failed", board, task_id)
        _reply(telegram, int(chat), int(message), t("inbound.error", id=task_id))
        return _skip()
    if not done:
        _reply(telegram, int(chat), int(message), t("inbound.changed"))
        return _skip()
    _reply(telegram, int(chat), int(message), render_done(board, task_id, held))
    return _skip()


def make_dispatch_hook(
    settings: Any,
    store: Any,
    api: Any,
    telegram: Any,
    clock: Callable[[], float] = time.time,
) -> Callable[..., Any]:
    """The ``pre_gateway_dispatch`` callback. Never raises; any internal error lets the message through.

    Async so Hermes awaits it on the gateway loop while the work (SQLite, Bot API) runs on a
    worker thread instead of stalling every other chat.
    """

    def run(event: Any) -> Optional[dict]:
        try:
            return handle_inbound(event, settings, store, api, telegram, clock())
        except Exception:
            log.exception("kanban-empowered: inbound reply handling failed; passing the message through")
            return None

    async def pre_gateway_dispatch(event: Any = None, **_ignored: Any) -> Optional[dict]:
        try:
            return await asyncio.to_thread(run, event)
        except Exception:
            log.exception("kanban-empowered: inbound reply handling failed; passing the message through")
            return None

    return pre_gateway_dispatch
