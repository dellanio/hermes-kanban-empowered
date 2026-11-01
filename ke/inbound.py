"""Authenticated two-way Telegram replies: "done" answered to a reminder unblocks the card.

Replies reach the plugin through Hermes' ``pre_gateway_dispatch`` hook, which runs BEFORE
the gateway's own authorization, so every sender must be treated as untrusted. The checks
run in this fixed order and nothing about a card is read before all of them pass:

1. the event is a direct Telegram message (not relayed, not from a bot, not synthetic, not
   an interactive-prompt answer, allowed to drive gateway control) replying to a message
   this plugin sent to that same chat (``sent_messages``), and its whole text is an explicit
   confirmation (``[n] done|feito|liguei|ok|approved|aprovado`` plus optional punctuation);
2. the sender's Telegram user id is the configured owner of the selected item
   (``settings.owner_ids[settings.owner_for(board)]``). Anybody else, a configured identity
   such as the ``escalate_to`` contact included, gets ``None``: no answer, nothing consumed,
   no state change, and the message continues to the normal gateway flow;
3. ``store.mark_update`` dedupes the update (a replay is consumed in silence);
4. the message is younger than ``settings.reply_ttl_s``.

From step 3 on the message is consumed: any later failure still returns skip.

Only then is the card read: it must still be ``blocked`` with the same block version as
the item in the message (the live newest ``blocked``/``gave_up`` event id, and the tracked wait row
when there is one), so a "done" answered to an old reminder can never resolve a newer block.

The same rules apply to replies to the live panel (``sent_messages`` kind "panel"), a message the
coordinator edits in place. Its numbering changes over time, so the reply is resolved against the
list that was current at the reply's own date (``panel.view_for_reply``); when the user may have
been looking at another list that differs at the referenced position the reply is consumed and
answered "the panel changed; reply again" instead of being applied. The panel mixes the boards of
several owners, so the sender must own EVERY item the reply could mean (otherwise ``None``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

from . import panel
from .messages import render_done, t
from .waits import block_version, held_behind

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "kanban-empowered"}
# The whole message must be the confirmation: an optional item number ("2", "2.", "2)", "2 -"),
# the word, optional trailing punctuation/emoji. "ok, but not yet" or "done?" is a conversation.
CHECK_MARK = "\N{WHITE HEAVY CHECK MARK}"
THUMBS_UP = "\N{THUMBS UP SIGN}"
VS16 = "\N{VARIATION SELECTOR-16}"
_TRAILING_MARKS = ".!" + CHECK_MARK + THUMBS_UP + VS16
REPLY_RE = re.compile(
    r"^\s*(?:([0-9]{1,4})\s*[.):\-]?\s*)?(feito|done|liguei|ok|aprovado|approved)"
    r"\s*[" + re.escape(_TRAILING_MARKS) + r"]*\s*$",
    re.IGNORECASE,
)
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


_logged_once: set[str] = set()


def _log_once(key: str, msg: str, *args: Any) -> None:
    if key not in _logged_once:
        _logged_once.add(key)
        log.warning(msg, *args)


def is_candidate(event: Any) -> bool:
    """Cheap, pure pre-filter (no I/O): could ``event`` be a reply to one of our reminders?"""
    source = getattr(event, "source", None)
    if source is None or not _is_telegram(getattr(source, "platform", None)):
        return False
    if getattr(event, "internal", False) or getattr(source, "is_bot", False):
        return False
    if getattr(source, "delivered_via_upstream_relay", False):
        return False  # our bot's own adapter delivers replies directly; never trust a relayed identity
    if getattr(event, "allow_gateway_control", True) is False:
        return False
    if getattr(event, "prompt_response", None):
        return False
    return _canonical_id(getattr(event, "reply_to_message_id", None)) is not None


def _replied_to_our_bot(event: Any, telegram: Any) -> bool:
    """False only when the raw Telegram update shows the replied-to message was authored by
    someone other than the bot that sent our reminders (a gateway on a different bot token).
    Without that information (no raw update, unknown bot id) the chat/message lookup stands."""
    bot_id = getattr(telegram, "bot_id", None)
    raw = getattr(event, "raw_message", None)
    author = getattr(getattr(getattr(raw, "reply_to_message", None), "from_user", None), "id", None)
    if bot_id is None or author is None:
        _log_once(
            "bot_author_unchecked",
            "kanban-empowered: cannot verify that replies answer this bot's own message "
            "(no raw Telegram update or no bot token); relying on the chat/message lookup",
        )
        return True
    return str(author) == str(bot_id)


def _owner_chat(settings: Any, item: dict) -> Optional[str]:
    """The canonical Telegram id of the configured owner of ``item``'s board, or ``None``."""
    return _canonical_id(settings.owner_ids.get(settings.owner_for(str(item["board"]))))


def _reply_ts(event: Any, now: float) -> float:
    """When the user wrote the message (Telegram's date), never later than ``now``; ``now`` when unknown."""
    stamp = getattr(event, "timestamp", None)
    try:
        value = float(stamp.timestamp()) if hasattr(stamp, "timestamp") else float(stamp)
    except (TypeError, ValueError, OverflowError, OSError):
        return float(now)
    return min(value, float(now))


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
    if not is_candidate(event):
        return None
    source = event.source
    chat = _canonical_id(getattr(source, "chat_id", None))
    sender = _canonical_id(getattr(source, "user_id", None))
    reply_to = _canonical_id(getattr(event, "reply_to_message_id", None))
    message = _canonical_id(getattr(event, "message_id", None))
    if chat is None or sender is None or reply_to is None or message is None:
        return None
    match = REPLY_RE.match(getattr(event, "text", None) or "")
    if match is None:
        return None  # not an explicit confirmation: a normal message for the agent
    sent = store.get_sent_message(int(chat), int(reply_to))
    if sent is None or sent.get("kind") not in ("waits", "panel"):
        return None
    if not _replied_to_our_bot(event, telegram):
        return None  # same chat and message id, but a message of another bot (DM ids are per bot)
    items = sent.get("items")
    if not isinstance(items, list) or (not items and sent["kind"] != "panel"):
        return None
    number = int(match.group(1)) if match.group(1) else None
    is_panel = sent["kind"] == "panel"
    ambiguous = False
    scope_items = None
    if is_panel:
        view = panel.view_for_reply(
            store, int(chat), int(reply_to), items, _reply_ts(event, now), number
        )
        items, ambiguous, scope_items = view.items, view.ambiguous, view.scope
        if not items:  # nothing was numbered then: only "the panel changed" can be said
            ambiguous = True
    if number is not None:
        selected = items[number - 1] if 1 <= number <= len(items) else None
    else:
        selected = items[0] if len(items) == 1 else None

    # -- 2. the sender is the configured owner of the selected item -------------------------
    if scope_items is not None and ambiguous:
        scope = scope_items
    else:
        scope = [selected] if selected is not None else items
    if is_panel:  # mixed owners: the sender must own every item the reply could mean
        if not scope:  # an empty panel: only the one it is sent to (the default owner)
            if _canonical_id(settings.owner_ids.get(settings.default_owner)) != sender:
                return None
        elif not all(_owner_chat(settings, item) == sender for item in scope):
            return None
    else:
        owners = {settings.owner_for(str(item["board"])) for item in scope}
        allowed = {_canonical_id(settings.owner_ids.get(o)) for o in owners} - {None}
        if sender not in allowed:
            return None  # not the owner (configured identity or stranger): untouched, flows on

    # -- 3. each update acts at most once ---------------------------------------------------
    if not store.mark_update(f"tg:{chat}:{message}", int(now)):
        return _skip()
    try:
        _act(event, source, settings, store, api, telegram, now, sent, items, selected, chat, sender, message, ambiguous)
    except Exception:
        log.exception("kanban-empowered: handling a consumed Telegram reply failed")
    return _skip()


def _act(
    event, source, settings, store, api, telegram, now, sent, items, selected, chat, sender, message, ambiguous=False
) -> None:
    """Everything after the update was consumed; the caller returns skip whatever happens here."""
    # -- 4. the request is still fresh ------------------------------------------------------
    if now - int(sent["created_at"]) >= settings.reply_ttl_s:
        _reply(telegram, int(chat), int(message), t("inbound.expired"))
        return
    if ambiguous:
        _reply(telegram, int(chat), int(message), t("inbound.panel_changed"))
        return
    if selected is None:
        _reply(telegram, int(chat), int(message), t("inbound.which_item", n=len(items)))
        return
    board, task_id, version = str(selected["board"]), str(selected["task_id"]), int(selected["version"])
    name = getattr(source, "user_name", None) or getattr(event, "user_name", None) or sender
    comment = t("inbound.comment.telegram", name=_clip(name), user_id=sender, text=_clip(event.text))
    try:
        done, held = resolve_wait(api, store, board, task_id, version, comment, now, f"telegram:{sender}")
    except Exception:
        log.exception("kanban-empowered: resolving %s/%s from Telegram failed", board, task_id)
        _reply(telegram, int(chat), int(message), t("inbound.error", id=task_id))
        return
    if not done:
        _reply(telegram, int(chat), int(message), t("inbound.changed"))
        return
    _reply(telegram, int(chat), int(message), render_done(board, task_id, held))


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
            if not is_candidate(event):  # pure checks on the loop; only candidates hop threads
                return None
            return await asyncio.to_thread(run, event)
        except Exception:
            log.exception("kanban-empowered: inbound reply handling failed; passing the message through")
            return None

    return pre_gateway_dispatch
