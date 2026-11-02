"""Authenticated two-way Telegram replies: the owner's answer to a reminder, linked to an item.

Replies reach the plugin through Hermes' ``pre_gateway_dispatch`` hook, which runs BEFORE
the gateway's own authorization, so every sender must be treated as untrusted. The checks
run in this fixed order and nothing about a card is read before all of them pass:

1. the event is a direct Telegram message (not relayed, not from a bot, not synthetic, not
   an interactive-prompt answer, allowed to drive gateway control) with some text, replying to
   a message this plugin sent to that same chat (``sent_messages``) and authored by our bot.
   The text names its item (``"<n> — <text>"``, ``"<n> – …"``, ``"<n> - …"``, ``"<n>: …"``,
   ``"<n>) …"``, ``"<n>. …"``, ``"<n> …"``); a message with a single item also takes bare text;
2. the sender's Telegram user id is the configured owner of the selected item
   (``settings.owner_ids[settings.owner_for(board)]``). Anybody else, a configured identity
   such as the ``escalate_to`` contact included, gets ``None``: no answer, nothing consumed,
   no state change, and the message continues to the normal gateway flow;
3. ``store.mark_update`` dedupes the update (a replay is consumed in silence);
4. the message is younger than ``settings.reply_ttl_s``.

From step 3 on the message is consumed: any later failure still returns skip.

Only then is the card read: it must still be ``blocked`` with the same block version as
the item in the message (the live newest ``blocked``/``gave_up`` event id, and the tracked wait row
when there is one), so an answer to an old reminder can never resolve a newer block. The text
after the item number is then classified against that item's ask (:func:`classify`):

a. one option letter the ask declares -> resolved, commented "choice: A — <label>";
b. one letter it does not declare -> answered "use a listed option", nothing changes;
c. a confirmation (one of the ask's reply words or a default one, trailing punctuation/emoji) -> resolved;
d. an explicit not-done ("não", "ainda não", "negativo", "no", "not yet", "nope"...) -> only a
   comment, the card stays blocked;
e. any other text -> it is the owner's answer: resolved, the text posted as a comment (1000 chars)
   so the agent continues with the feedback;
f. several items and no number -> "which item?" (nothing changes).

Steps a-e live in :func:`apply_answer`, shared with the Desktop dashboard's ``POST /answer`` (``ke.dashboard``),
so an answer means the same thing wherever it is typed.

A bare confirmation for an item that asks for a choice (it has options) also changes nothing: the
reply asks for one of the listed letters. Replies to the pinned panel count only with an item
number (anything else passes through to the agent); on a one-item reminder a leading number other
than 1 is part of the text.

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
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import panel
from .asks import CONFIRM_WORDS, parse_ask, reply_words
from .messages import EN, _clip_to, active_language, board_title, branded, render_answer, reply_hint, short_action, t
from .waits import WAIT_STATUSES, block_version, current_block, display_name, held_behind

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "kanban-empowered"}
# "<n> — text", "<n> – text", "<n> - text", "<n>: text", "<n>) text", "<n>. text", "<n> text", "<n>"
# ("." and ")" need a space after them, so "1.5 GB" is not item 1)
ITEM_RE = re.compile(r"^\s*([0-9]{1,4})(?:\s*[—–\-:]\s*|[.)]\s+|[.)]?\s*$|\s+)(.*)$", re.DOTALL)
_LETTER_RE = re.compile(r"^([A-Za-z])\s*[.!)]*$")
# several letters at once ("A/B", "A ou B", "A, B", "A or B"): not a choice
_LETTERS_RE = re.compile(r"^[A-Za-z](?:\s*(?:/|,|;|\bou\b|\bor\b|\be\b|\band\b)\s*[A-Za-z])+\s*[.!?]*$", re.IGNORECASE)
# An explicit "not done": the card stays blocked. In Portuguese a bare "no" counts only when it
# stands alone ("no celular..." means "on the phone..."); in any other language "no ..." counts.
_NEGATION_COMMON = r"ainda\s+n[ãa]o|n[ãa]o|negativo|not\s+yet|not|nope|never"
NEGATION_RE_PT = re.compile(rf"^(?:{_NEGATION_COMMON}|no(?=\s*(?:$|[,.;:!])))(?!\w)", re.IGNORECASE)
NEGATION_RE_EN = re.compile(rf"^(?:{_NEGATION_COMMON}|no)(?!\w)", re.IGNORECASE)


def _negation_re():
    return NEGATION_RE_PT if active_language().lower().startswith("pt") else NEGATION_RE_EN
_PLACEHOLDER_RE = re.compile(r"^<[^<>]*>[\W_]*$")  # "<your answer>": the footer example typed back
_TRAILING_RE = re.compile(r"[^\w?]+$")  # trailing punctuation and emoji (a "?" makes it a question)
_ID_RE = re.compile(r"-?[1-9][0-9]{0,19}")
_COMMENT_TEXT_MAX = 500
_ANSWER_TEXT_MAX = 1000
_ACTION_SHOWN = 160

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


def _clip(text: str, limit: int = _COMMENT_TEXT_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_reply(text: str) -> tuple[Optional[int], str]:
    """``(item number or None, the rest)`` of a reply text (pure)."""
    m = ITEM_RE.match(text or "")
    if m is None:
        return None, (text or "").strip()
    return int(m.group(1)), m.group(2).strip()


_NEUTRAL_PT = "Responda a esta mensagem quando concluir, ou diga o que falta."  # recognised whatever the language


def _squash(text: str) -> str:
    return " ".join(_TRAILING_RE.sub("", " ".join(str(text).split())).split()).casefold()


def is_placeholder(text: str) -> bool:
    """True for a reply that is the footer's example typed back (``<your answer>``, any ``<…>`` body) or the
    neutral reply instruction itself: it carries no answer."""
    text = (text or "").strip()
    if _PLACEHOLDER_RE.match(text):
        return True
    return _squash(text) in {_squash(t("waits.reply.neutral")), _squash(EN["waits.reply.neutral"]), _squash(_NEUTRAL_PT)}


def classify(text: str, ask: Any) -> tuple[str, Any]:
    """What a reply means for an item with ``ask`` (``None`` for an unstructured block).

    ``("empty", None)``, ``("placeholder", None)`` (the footer example or the neutral instruction typed back:
    nothing changes, the owner is asked to reply again), ``("option", (letter, label))``, ``("unknown_option", letter)``,
    ``("need_option", None)`` (a bare confirmation for an item that asks for a choice),
    ``("confirm", None)``, ``("negation", None)`` or ``("answer", None)`` (see the module doc).
    """
    text = (text or "").strip()
    if not text:
        return "empty", None
    if is_placeholder(text):
        return "placeholder", None
    m = _LETTER_RE.match(text)
    if m:
        letter = m.group(1).upper()
        label = ask.option(letter) if ask is not None else None
        return ("option", (letter, label)) if label is not None else ("unknown_option", letter)
    if ask is not None and ask.options and _LETTERS_RE.match(text):
        return "need_option", None  # "A/B" names no single option: nothing changes
    word = _TRAILING_RE.sub("", text).strip().lower()
    if word and (word in CONFIRM_WORDS or word in reply_words(ask)):
        if ask is not None and ask.options:
            return "need_option", None  # "ok" does not say which option: nothing changes
        return "confirm", None
    if _negation_re().match(text):
        return "negation", None
    return "answer", None


def _reply(telegram: Any, chat_id: int, message_id: int, text: str) -> None:
    if telegram is None:
        log.warning("kanban-empowered: Hermes resolved no TELEGRAM_BOT_TOKEN; cannot answer a reply")
        return
    try:
        telegram.send(chat_id, branded(text), reply_to=message_id)
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
            if task is None or task.status not in WAIT_STATUSES:
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
    text = getattr(event, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None  # nothing to read (a sticker, a photo without caption): a normal message
    number, body = split_reply(text)
    sent = store.get_sent_message(int(chat), int(reply_to))
    if sent is None or sent.get("kind") not in ("waits", "panel"):
        return None
    if not _replied_to_our_bot(event, telegram):
        return None  # same chat and message id, but a message of another bot (DM ids are per bot)
    items = sent.get("items")
    if not isinstance(items, list) or (not items and sent["kind"] != "panel"):
        return None
    is_panel = sent["kind"] == "panel"
    if is_panel and number is None:
        return None  # the pinned panel only takes numbered answers; anything else is for the agent
    if not is_panel and len(items) == 1 and number is not None and number != 1:
        number, body = None, text.strip()  # "2 cabos faltando" on a one-item reminder is plain text
    if number is None:
        body = text.strip()
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
        _act(event, source, settings, store, api, telegram, now, sent, items, selected, chat, sender, message,
             ambiguous, body)
    except Exception:
        log.exception("kanban-empowered: handling a consumed Telegram reply failed")
    return _skip()


def _act(
    event, source, settings, store, api, telegram, now, sent, items, selected, chat, sender, message, ambiguous=False,
    body=None,
) -> None:
    """Everything after the update was consumed; the caller returns skip whatever happens here.

    ``body`` is the reply without its message-local item number (what is classified and commented).
    """
    # -- 4. the request is still fresh ------------------------------------------------------
    if now - int(sent["created_at"]) >= settings.reply_ttl_s:
        _reply(telegram, int(chat), int(message), t("inbound.expired"))
        return
    if ambiguous:
        _reply(telegram, int(chat), int(message), t("inbound.panel_changed"))
        return
    if selected is None:
        _reply(telegram, int(chat), int(message), t("inbound.which_item", n=len(items), word=_first_hint(api, items)))
        return
    board, task_id, version = str(selected["board"]), str(selected["task_id"]), int(selected["version"])
    body = (event.text or "").strip() if body is None else body
    name = _clip(getattr(source, "user_name", None) or getattr(event, "user_name", None) or sender)
    outcome = apply_answer(
        api, store, board, task_id, version, body, now,
        detail=f"telegram:{sender}",
        resolved_comment=lambda said: t("inbound.comment.telegram", name=name, user_id=sender, text=said),
        note_comment=lambda text: t("inbound.comment.note", name=name, user_id=sender, text=text),
    )
    _reply(telegram, int(chat), int(message), outcome.message)


@dataclass(frozen=True)
class Answer:
    """What a reply did to its item: ``result`` is ``resolved`` | ``noted`` | ``ambiguous`` | ``changed`` |
    ``error``, ``message`` the confirmation for the person (translated), ``held`` the cards that were waiting
    on a resolved item and ``kind`` the classification (:func:`classify`)."""

    result: str
    message: str
    held: tuple = ()
    kind: str = ""


def apply_answer(
    api: Any,
    store: Any,
    board: str,
    task_id: str,
    version: int,
    body: str,
    now: float,
    *,
    detail: str,
    resolved_comment: Callable[[str], str],
    note_comment: Callable[[str], str],
) -> Answer:
    """Classify ``body`` against the item's ask and apply it: THE resolution shared by every reply channel.

    The Telegram reply (after its authentication, dedupe and TTL checks) and the Desktop dashboard's
    ``POST /answer`` both land here, so a reply means the same thing wherever it is typed. The live block
    version is checked again under the resolve lock (:func:`resolve_wait` / :func:`note_wait`).
    ``resolved_comment(said)`` / ``note_comment(text)`` word the card comment of the channel; ``detail`` is
    the audit detail of the ``human_done`` / ``human_note`` action.
    """
    try:
        live, ask, reason, card_title = _item_ask(api, board, task_id, version)
    except Exception:
        log.exception("kanban-empowered: reading %s/%s for a reply failed", board, task_id)
        return Answer("error", t("inbound.error"))
    if not live:
        return Answer("changed", t("inbound.changed"))
    kind, value = classify(body, ask)
    if kind in ("empty", "placeholder"):
        return Answer("ambiguous", t("reply.empty", word=reply_hint(ask)), kind=kind)
    if kind == "unknown_option":
        if ask is not None and ask.options:
            text = t("reply.option_unknown", letter=value, options=", ".join(k for k, _ in ask.options))
        else:
            text = t("reply.option_none")
        return Answer("ambiguous", text, kind=kind)
    if kind == "need_option":
        options = ", ".join(k for k, _ in ask.options)
        return Answer("ambiguous", t("reply.option_needed", options=options), kind=kind)
    if kind == "negation":
        note = note_comment(_clip(body, _ANSWER_TEXT_MAX))
        try:
            noted = note_wait(api, store, board, task_id, version, note, now, detail)
        except Exception:
            log.exception("kanban-empowered: noting a reply on %s/%s failed", board, task_id)
            return Answer("error", t("inbound.error"), kind=kind)
        return Answer("noted", t("reply.noted"), kind=kind) if noted else Answer("changed", t("inbound.changed"), kind=kind)
    if kind == "option":
        said = t("inbound.comment.choice", letter=value[0], label=_clip(value[1]))
    elif kind == "answer":
        said = _clip(body, _ANSWER_TEXT_MAX)
    else:
        said = _clip(body)
    try:
        done, held = resolve_wait(api, store, board, task_id, version, resolved_comment(said), now, detail)
    except Exception:
        log.exception("kanban-empowered: resolving %s/%s from a reply failed", board, task_id)
        return Answer("error", t("inbound.error"), kind=kind)
    if not done:
        return Answer("changed", t("inbound.changed"), kind=kind)
    board_label = board_title(board, display_name(api, board), ask.subject if ask is not None else "")
    action = _clip_to(short_action(ask, reason, card_title), _ACTION_SHOWN)
    message = render_answer(board_label, action, len(held), choice=value if kind == "option" else None)
    return Answer("resolved", message, tuple(held), kind)


def _first_hint(api: Any, items: list) -> str:
    """The reply example for "which item?": the first item's own word (a neutral placeholder when it has no ask)."""
    try:
        item = items[0]
        _live, ask, _reason, _title = _item_ask(api, str(item["board"]), str(item["task_id"]), int(item["version"]))
        return reply_hint(ask) if _live else t("waits.default_word")
    except Exception:
        return t("waits.default_word")


def _item_ask(api: Any, board: str, task_id: str, version: int) -> tuple[bool, Any, str, str]:
    """``(still blocked on version, its ask or None, the raw block reason, the card title)`` of a selected item."""
    with api.connect(board) as conn:
        task = api.task(conn, task_id)
        if task is None or task.status not in WAIT_STATUSES:
            return False, None, "", ""
        block = current_block(task, api.events(conn, task_id))
        if block is None or block.version != int(version):
            return False, None, "", ""
        return True, parse_ask(block.reason), block.reason, str(getattr(task, "title", "") or "")


def note_wait(
    api: Any, store: Any, board: str, task_id: str, version: int, comment: str, now: float, detail: str
) -> bool:
    """Comment on ``task_id`` if it is still blocked on block ``version``; the card stays blocked."""
    with _resolve_lock:
        with api.connect(board) as conn:
            task = api.task(conn, task_id)
            if task is None or task.status not in WAIT_STATUSES:
                return False
            if block_version(api.events(conn, task_id)) != int(version):
                return False
            wait = store.latest_wait(board, task_id)
            if wait is not None and int(wait["version"]) != int(version):
                return False
            api.comment(conn, task_id, comment)
    try:
        store.log_action(board, task_id, "human_note", detail, int(now))
    except Exception:
        log.exception("kanban-empowered: could not log human_note for %s/%s", board, task_id)
    return True


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
