"""User-facing text: the plugin's i18n layer.

Every human-facing string is a catalog key. ``t(key, **kw)`` asks Hermes first
(``agent.i18n.t`` through ``hermes_api.translate``), so the text follows the
user's Hermes language, and falls back to the built-in English catalog below
when Hermes has no translation for the key. The same English text ships as
``locales/en.yaml`` and the first translation as ``locales/pt-br.yaml``
(declared in ``plugin.yaml`` through ``provides_locales``); a test keeps the
three key sets identical. Keys here omit the ``kanban_empowered.`` prefix that
the YAML catalogs and Hermes use.
"""
from __future__ import annotations

import logging
import re
import time

PREFIX = "kanban_empowered."

EN: dict[str, str] = {
    # ages
    "age.s": "{n} s",
    "age.min": "{n} min",
    "age.h": "{n} h",
    "age.d": "{n} d",
    # doctor
    "doctor.present": "present",
    "doctor.missing": "missing",
    "doctor.none": "none",
    "doctor.unknown": "unknown",
    "doctor.config_invalid": "invalid ({error})",
    "doctor.token.present": "present",
    "doctor.token.missing": "missing",
    "doctor.owner_chats": "{n} configured",
    "doctor.telegram_path.host": "{path} (hostname)",
    "doctor.telegram_path.ip": "{path} (IPv4 fallback)",
    "doctor.conflict.override": "form value {form_value} overrides config.yaml {config_key} {config_value}",
    "doctor.conflict.owner_id": "telegram_owner_id {form_value} is ignored: telegram.owner_ids is set and has no id for {owner}",
    "doctor.conflict.owner_id_differs": "telegram_owner_id {form_value} is ignored: config.yaml {config_key} is {config_value}",
    # explain: header and lookup
    "explain.header": "{id}  {title}  {status} {age} ago",
    "explain.not_found": "card {card} not found {where}",
    "explain.where_board": "on board {board}",
    "explain.where_any": "on any board",
    "explain.board_error": "error reading board {board}: {error}",
    "explain.ambiguous": "card {card} exists on several boards ({boards}); use --board",
    "explain.unknown_card": "card {card} not found on board {board}",
    # explain: conditions
    "explain.dependencies.none": "no open dependencies",
    "explain.dependencies.waiting": "waiting on {id} ({label}, {age}, owner: {owner})",
    "explain.dependencies.no_owner": "no owner",
    "explain.guard.none": "no respawn guard",
    "explain.guard.active": "guarded: {reason} ({count}x, for {age})",
    "explain.guard.unknown_reason": "unknown reason",
    "explain.live.none": "no active run",
    "explain.live.ok": "run #{run}, worker pid {pid}, {heartbeat}",
    "explain.live.no_pid": "run #{run} has no worker pid, {heartbeat}",
    "explain.live.heartbeat": "heartbeat {age} ago",
    "explain.live.no_heartbeat": "no heartbeat yet",
    "explain.fresh.none": "no summary yet",
    "explain.fresh.clean": "summary {age} ago, nothing new since",
    "explain.fresh.stale": "summary {age} ago; {news} since",
    "explain.fresh.and": "{first} and {second}",
    "explain.comment.one": "{n} comment",
    "explain.comment.many": "{n} comments",
    "explain.event.one": "{n} event",
    "explain.event.many": "{n} events",
    "explain.wait.none": "no wait recorded",
    "explain.wait.active": "waiting on {owner} for {age}; next reminder in {next}",
    # status
    "status.running": "running: {n}",
    "status.human_waits": "waiting on a human: {n}",
    "status.guarded": "guarded: {n}",
    "status.resolved_today": "resolved today: {n}",
    "status.boards": "boards: {boards}",
    "status.no_boards": "none",
    "status.row.running": "  running  {board}/{id}  {title}  ({assignee}, {age} ago{heartbeat})",
    "status.row.heartbeat": ", heartbeat {age} ago",
    "status.row.no_assignee": "no assignee",
    "status.row.wait": "  waiting  {board}/{id}  {title}  (owner: {owner}, {age} ago)",
    "status.row.guarded": "  guarded  {board}/{id}  {title}  ({reason}, {count}x, for {age})",
    # natural ages (Telegram): one unit, full words
    "age.nat.min": "{n} min",
    "age.nat.hour.one": "{n} hour",
    "age.nat.hour.many": "{n} hours",
    "age.nat.day.one": "{n} day",
    "age.nat.day.many": "{n} days",
    # waits (human reminders over Telegram)
    "waits.header": "📋 KANBAN · NEEDS YOU",
    "waits.counts": "{pending} · {blocked}",
    "waits.pending.one": "{n} pending",
    "waits.pending.many": "{n} pending",
    "waits.blocked.one": "{n} blocked card",
    "waits.blocked.many": "{n} blocked cards",
    "waits.status": "⏳ Stalled for {age}",
    "waits.status.blocks": "⏳ Stalled for {age} · blocks {cards}",
    "waits.cards.one": "{n} card",
    "waits.cards.many": "{n} cards",
    "waits.action": "👉 {text}",
    "waits.option": "{letter} — {label}",
    "waits.reply.word": "Then reply: “{word}”.",
    "waits.next_step": "Next step: {text}",
    "waits.footer": "↩️ When replying, name the item:",
    "waits.footer.example": "“{n} — {word}”",
    "waits.copy.lead": "Copy for follow-up",
    "waits.default_word": "done",
    "waits.you": "you",
    "waits.no_reason": "(no reason given)",
    # inbound replies (Telegram) and the done command
    "inbound.which_item": "Which item? Start your reply with its number, from 1 to {n} (e.g. “1 — {word}”).",
    "inbound.changed": "This request has changed since that message; nothing was changed.",
    "inbound.expired": "This request has expired; nothing was changed.",
    "inbound.error": "Something went wrong while processing this reply; check the item before replying again.",
    "inbound.unblocked": "✅ Unblocked {board} · {id}.",
    "inbound.held.none": "Nothing else is waiting on it.",
    "inbound.held.one": "Next in line behind it: 1 card ({ids}).",
    "inbound.held.many": "Next in line behind it: {n} cards ({ids}).",
    "inbound.comment.telegram": "unblocked via Telegram by {name} ({user_id}): {text}",
    "inbound.comment.choice": "choice: {letter} — {label}",
    "inbound.comment.note": "noted via Telegram from {name} ({user_id}), still waiting: {text}",
    "inbound.comment.cli": "unblocked via CLI by {user}",
    "reply.resolved": "✅ {board}: {action}",
    "reply.choice": "Choice: {letter} — {label}",
    "reply.held.none": "Nothing else was waiting on it.",
    "reply.held.one": "1 card was waiting on it and can move on now.",
    "reply.held.many": "{n} cards were waiting on it and can move on now.",
    "reply.noted": "Noted; the item is still waiting.",
    "reply.option_unknown": "{letter} is not one of this item's options; reply with one of: {options}.",
    "reply.option_none": "This item has no options; reply with a word or a sentence.",
    "reply.option_needed": "This item asks for a choice; reply with one of: {options}.",
    "reply.empty": "Write your answer after the item number, e.g. “1 — {word}”.",
    "done.no_wait": "card {card} on board {board} has no open human wait; nothing was changed",
    # guards: alert about cards held by a respawn guard, and the release command
    "guards.head": "🔒 KANBAN · STUCK CARDS",
    "guards.action.one": "👉 This card is stuck behind a respawn guard. Hand it to another profile with the command below.",
    "guards.action.many": "👉 {n} cards are stuck behind a respawn guard. Hand each one to another profile with the command below it.",
    "guards.item.detail": "⏳ Held for {age} · {reason} · assigned to {assignee}",
    "guards.item.pr": "Latest PR: {url} (commented {when})",
    "guards.item.release": "hermes kanban-empowered release {id} --to <profile>",
    "guards.no_assignee": "nobody",
    "guards.reason.active_pr": "a PR is already open",
    "guards.reason.blocker_auth": "a quota or auth blocker",
    "release.done": "released: assigned to {to}",
    "release.no_profile": "profile {to} does not exist",
    "release.same_profile": "handing off to the same profile does not release the guard",
    "release.no_exit": "no compatible exit: {reason}",
    "release.no_guard": "no guard",
    "release.running": "card is running; release not possible now",
    "release.comment": "released the respawn guard ({reason}): assigned to {to} (was {previous}) by {actor}",
    # infra recovery: the comment on an undone breaker block, and the alerts to the owner
    "infra.undo_comment.one": "block undone: {n} infrastructure failure ({pattern}), not a problem with the work",
    "infra.undo_comment.many": "block undone: {n} infrastructure failures ({pattern}), not a problem with the work",
    "infra.head": "🛠️ KANBAN · INFRASTRUCTURE",
    "infra.budget.action": "👉 Check the infrastructure (gateway, workers), then unblock this card yourself.",
    "infra.budget.detail": "Blocked again by infrastructure failures. The automatic undo budget ({count} in {window}) is used up; I will not retry.",
    "infra.budget.how": "Details: hermes kanban-empowered explain {id} --board {board}",
    "infra.systemic": "👉 Check the host (gateway, persistence): {n} workers exited for infrastructure reasons in {window}, not because of their work.",
    # live panel (pinned Telegram message, terminal watch)
    "panel.title": "📋 KANBAN NOW · {time}",
    "panel.counts": "{running} running · {held} on hold · {pending}",
    "panel.nothing": "Nothing needs you right now.",
    "panel.more": "… and {n} more",
    "panel.resolved": "Resolved on its own today: {n}",
    "panel.resolved.last": "Last: {board} at {time}",
    "panel.guards": "🔒 Long respawn guards: {n}",
    "panel.term.title": "Kanban now · {time}",
    "panel.term.counts": "{running} running · {held} held · {waits} depend on you",
    "panel.term.needs": "Needs you",
    "panel.term.item": "  {n}. {board}/{id}: {need}  ({age}, holding {held})",
    "panel.term.resolved.last": "Last: {board} · {id} · {time}",
    "panel.term.running": "  running  {board}/{id}  {title}  ({assignee}, {age} ago)",
    "panel.term.held": "  held     {board}/{id}  ({reason}, for {age})",
    "panel.term.how": "Resolve an item with: hermes kanban-empowered done <id>",
    "inbound.panel_changed": "The panel changed; reply again.",
    "status.watch.hint": "Refreshing every {n} s. Press Ctrl+C to exit.",
    "status.watch.error": "Could not read the state: {error}",
    # agent-facing: the worker guard's veto of an unstructured human block
    "guard.block": "kanban-empowered: a block that waits on a person needs a structured ask.\n{template}",
    "guard.template": (
        "Use these lines in the block reason; the first three are required:\n"
        "Need: <the action you need from the person, at most two sentences, no card ids>\n"
        "Why: <why the work cannot move without it, one short sentence>\n"
        "How to reply: <the word the person should reply, e.g. reply \"done\">\n"
        "Subject: <optional: a short topic shown next to the board name>\n"
        "Next step: <optional: what you will do once answered>\n"
        "Options: <optional: one choice per line below>\n"
        "A — <first choice>\n"
        "B — <second choice>"
    ),
    # setup alerts (Telegram): one short message per issue, never inside a reminder
    "config.head": "⚙️ KANBAN · SETUP",
    "config.veto": (
        "👉 Re-apply the kanban veto hooks patch to this Hermes build, then check with: "
        "hermes kanban-empowered doctor\nUntil then the guards run in cooperative mode only."
    ),
    "config.escalation_no_chat": (
        "👉 Add a Telegram chat id for the escalation contact (telegram.owner_ids).\n"
        "It is configured but has no chat id, so escalations reach nobody else."
    ),
}


# Language override (``settings.language``): None follows the Hermes language.
_LANG = None
# Configured role names (owner_ids keys, escalate_to): replaced by "you" in agent-written text.
_ROLES: frozenset = frozenset()
_log = logging.getLogger(__name__)


def set_language(lang) -> None:
    """Force the language of the plugin's messages (``"pt"``, ``"en"``...); ``None`` follows Hermes.

    The value is checked with Hermes (``resolve_language_id``): a language Hermes does not support
    is logged once and ignored (the messages follow Hermes). When Hermes cannot be asked, the value
    is kept as given.
    """
    global _LANG
    value = (str(lang).strip() or None) if lang else None
    if value is not None:
        try:
            from . import hermes_api

            resolved = hermes_api.resolve_language(value)
        except Exception:
            resolved = value
        if not resolved:
            _log.warning("kanban-empowered: language %r is not supported by Hermes; following Hermes", value)
        value = resolved or None
    _LANG = value


def active_language() -> str:
    """The language the plugin's messages are in: the override, else Hermes', else ``en``."""
    if _LANG:
        return _LANG
    try:
        from . import hermes_api

        return hermes_api.language() or "en"
    except Exception:
        return "en"


def set_roles(names) -> None:
    """The configured role names that must never reach Telegram (owner_ids keys, escalate_to)."""
    global _ROLES
    _ROLES = frozenset(str(n).strip() for n in (names or ()) if n and str(n).strip())


def _translate(full_key: str, **kw):
    """Hermes' translation, or ``None``. The adapter is imported here, not at module load, so the
    built-in English text (the worker guard's veto above all) survives a Hermes module that moved."""
    try:
        from . import hermes_api
    except Exception:
        return None
    if _LANG:
        return hermes_api.translate(full_key, lang=_LANG, **kw)
    return hermes_api.translate(full_key, **kw)


def t(key: str, **kw) -> str:
    """Text for ``key`` in the user's Hermes language (or ``settings.language``), English when Hermes has none.

    ``kw`` are format arguments; they must not be named ``lang`` or ``key`` (``agent.i18n.t``
    takes ``lang`` itself, and ``key`` is this function's own parameter).
    """
    out = _translate(PREFIX + key, **kw)
    if out is not None:
        return out
    text = EN.get(key)
    if text is None:
        return key
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def plural(key: str, n: int, **kw) -> str:
    """``<key>.one`` for 1, ``<key>.many`` otherwise."""
    return t(f"{key}.one" if n == 1 else f"{key}.many", n=n, **kw)


def human_age(seconds: int) -> str:
    """``5 h 20 min``, ``40 min``, ``2 d 3 h``, ``12 s`` (the two most significant units)."""
    s = max(0, int(seconds))
    if s < 60:
        return t("age.s", n=s)
    if s < 3600:
        return t("age.min", n=s // 60)
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{t('age.h', n=h)} {t('age.min', n=m)}" if m else t("age.h", n=h)
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{t('age.d', n=d)} {t('age.h', n=h)}" if h else t("age.d", n=d)


def natural_age(seconds: int) -> str:
    """``14 days``, ``15 hours``, ``40 min``: the most significant unit only, in words (Telegram)."""
    s = max(0, int(seconds))
    if s >= 86400:
        return plural("age.nat.day", s // 86400)
    if s >= 3600:
        return plural("age.nat.hour", s // 3600)
    return t("age.nat.min", n=max(1, s // 60))


# -- Telegram text hygiene: no card ids, hashes or CLI commands ---------------------------
_CARD_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# a standalone hex run of 7+ characters (either case) with at least one digit and one a-f letter (a hash)
_HASH_RE = re.compile(
    r"(?<![\w/.\-])(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}(?![\w/\-])"
)
_CODE_CLI_RE = re.compile(r"`[^`\n]*\bhermes\s+kanban[^`\n]*`")
# "hermes kanban-empowered done …", "hermes kanban show …": case-sensitive, a lower-case subcommand
# must follow, so prose such as "Abra o Hermes Kanban e aprove o card" is left alone
_CLI_RE = re.compile(
    r"(?:\b(?:[Rr]un|[Rr]ode|[Ee]xecute|[Uu]se)\s*:?\s*)?\bhermes\s+kanban(?:-empowered)?\s+[a-z][a-z\-]*\b[^.;\n]*"
)
_LETTER_RE = re.compile(r"[^\W\d_]")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=\S)")


def strip_ids(text) -> str:
    """``text`` on one line without card ids, hashes or ``hermes kanban…`` commands (tidied up)."""
    text = " ".join(str(text or "").split())
    text = _CODE_CLI_RE.sub("", text)
    text = _CLI_RE.sub("", text)
    text = _CARD_ID_RE.sub("", text)
    text = _UUID_RE.sub("", text)
    text = _HASH_RE.sub("", text)
    # tidy what the removals left behind
    text = re.sub(r"\(\s*[,;]?\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\s+([.,;:!?)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"[,;:]+(?=[.!?])", "", text)
    text = re.sub(r"([,;])(?:\s*[,;])+", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.strip(" ,;")


# Portuguese article/contraction in front of a role name -> what stays in front of "você"
_PT_BEFORE_YOU = {
    "do": "de", "da": "de", "dos": "de", "das": "de",
    "no": "em", "na": "em", "nos": "em", "nas": "em",
    "pelo": "por", "pela": "por", "pelos": "por", "pelas": "por",
    "ao": "a", "aos": "a", "à": "a", "às": "a",
    "o": "", "os": "",
}


def replace_roles(text) -> str:
    """Configured role names in agent-written text become the reader ("você"/"you", whole word, any
    case). In Portuguese the article or contraction in front is adapted ("do sponsor" -> "de você",
    "pelo sponsor" -> "por você", "o sponsor" -> "você"; "a sponsor" keeps its "a"); in other
    languages a leading "the" goes with the name."""
    text = str(text or "")
    if not _ROLES:
        return text
    names = "|".join(re.escape(r) for r in sorted(_ROLES, key=len, reverse=True))
    you = t("waits.you")
    if active_language().lower().startswith("pt"):
        before = "|".join(sorted(_PT_BEFORE_YOU, key=len, reverse=True))

        def pt(m):
            word = m.group(1)
            if not word:
                return you
            lead = _PT_BEFORE_YOU[word.lower()]
            if not lead:
                return you
            if word[:1].isupper():
                lead = lead[:1].upper() + lead[1:]
            return f"{lead} {you}"

        return re.sub(rf"(?:\b({before})\s+)?\b(?:{names})\b", pt, text, flags=re.IGNORECASE)
    return re.sub(rf"(?:\bthe\s+)?\b(?:{names})\b", you, text, flags=re.IGNORECASE)


def agent_text(text) -> str:
    """Agent-written text as Telegram may show it: role names replaced, ids/hashes/commands removed."""
    out = strip_ids(replace_roles(text))
    return out if _LETTER_RE.search(out) else ""


def sentences(text: str, n: int = 2) -> str:
    """The first ``n`` sentences of ``text``."""
    parts = _SENTENCE_RE.split(text.strip())
    return " ".join(parts[:n])


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def circled(n: int) -> str:
    """①..⑳, then ``(21)``, ``(22)``…"""
    return _CIRCLED[n - 1] if 1 <= n <= len(_CIRCLED) else f"({n})"


def board_title(board: str, name: str = "", subject: str = "") -> str:
    """``VOZ HERMES`` (the display name, else the slug, upper-cased, ``-``/``_`` as spaces) [`` · SUBJECT``]."""
    base = _clip_to(str(name or board).replace("-", " ").replace("_", " "), _NAME_MAX).upper()
    subject = _clip_to(strip_ids(replace_roles(subject)), _NAME_MAX)
    return f"{base} · {subject.upper()}" if subject else base


_NAME_MAX = 60  # characters of a board name or a subject in a title


# -- human wait reminders ---------------------------------------------------------
_RAW_REASON_MAX = 600
_HELD_IDS_SHOWN = 5
_ACTION_MAX = 300
_CONTEXT_MAX = 200
_OPTION_MAX = 120
SEPARATOR = "-" * 38


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _RAW_REASON_MAX else text[: _RAW_REASON_MAX - 1] + "…"


def _sentence_end(text: str) -> str:
    return text if not text or text[-1] in ".!?…" else text + "."


def short_action(ask, reason: str = "") -> str:
    """The request in at most two sentences, without ids (the raw reason for an unstructured block)."""
    raw = ask.need if ask is not None else reason
    text = _clip_to(sentences(agent_text(raw)), _ACTION_MAX)
    return text or t("waits.no_reason")


def reply_hint(ask) -> str:
    """What a reply to this item looks like: ``A`` for an ask with options, else its primary word."""
    from .asks import primary_word

    if ask is not None and ask.options:
        return ask.options[0][0]
    return primary_word(ask) or t("waits.default_word")


def _reply_line(ask) -> str:
    from .asks import primary_word, reply_is_plain

    if ask is None or (reply_is_plain(ask) and primary_word(ask)):
        return t("waits.reply.word", word=reply_hint(ask))
    text = _clip_to(sentences(agent_text(ask.reply)), _ACTION_MAX)
    if not text:
        return t("waits.reply.word", word=reply_hint(ask))
    return _sentence_end(text[:1].upper() + text[1:])


def _context_line(ask) -> str:
    if ask is None:
        return ""
    if ask.next_step and agent_text(ask.next_step):
        return _clip_to(t("waits.next_step", text=agent_text(ask.next_step)), _CONTEXT_MAX)
    return _clip_to(agent_text(ask.why), _CONTEXT_MAX)


def _wait_item(due, number: int, copy: bool = False) -> list[str]:
    """One item: title, status, action (+ options or reply instruction), context."""
    ask = due.ask
    title = board_title(due.board, getattr(due, "board_name", ""), ask.subject if ask is not None else "")
    held = len(due.held_behind)
    age = natural_age(due.age_s)
    status = (
        t("waits.status.blocks", age=age, cards=plural("waits.cards", held)) if held else t("waits.status", age=age)
    )
    lines = [f"{circled(number)} {title}" if number else title, status, ""]
    action = short_action(ask, due.reason)
    if ask is not None and ask.options:
        if action[-1:] in ".;:!":
            action = action[:-1]
        if action[-1:] != "?":
            action += ":"
        lines += [t("waits.action", text=action), ""]
        lines += [t("waits.option", letter=k, label=_clip_to(strip_ids(replace_roles(v)), _OPTION_MAX))
                  for k, v in ask.options]
    else:
        lines.append(t("waits.action", text=action))
        if not copy:
            lines.append(_reply_line(ask))
    context = _context_line(ask)
    if context:
        lines += ["", context]
    return lines


def render_waits(owner: str, dues: list, copy: bool = False) -> str:
    """The reminder message: a header, then one block per item (numbered when there are several).

    ``owner`` is not shown (role names never reach Telegram). ``copy`` renders the escalated copy
    for the ``escalate_to`` contact: a "copy for follow-up" lead, the same items, no reply
    instructions (a reply from the contact is never accepted).
    """
    if not dues:
        return ""
    held = {tid for d in dues for tid in d.held_behind}
    lines = [t("waits.copy.lead")] if copy else []
    lines += [
        t("waits.header"),
        t("waits.counts", pending=plural("waits.pending", len(dues)), blocked=plural("waits.blocked", len(held))),
        "",
    ]
    many = len(dues) > 1
    for n, due in enumerate(dues, start=1):
        if n > 1:
            lines += ["", SEPARATOR]
        lines += _wait_item(due, n if many else 0, copy=copy)
    if many and not copy:
        lines += ["", t("waits.footer")]
        lines += [t("waits.footer.example", n=n, word=reply_hint(d.ask)) for n, d in enumerate(dues, start=1)]
    return "\n".join(lines)


def render_done(board: str, task_id: str, held: list) -> str:
    """CLI confirmation after a human resolved a wait: the card and what is next in line behind it."""
    held = list(held)
    if not held:
        tail = t("inbound.held.none")
    else:
        shown = held[:_HELD_IDS_SHOWN]
        ids = ", ".join(shown) + (", …" if len(held) > len(shown) else "")
        tail = t("inbound.held.one", ids=ids) if len(held) == 1 else t("inbound.held.many", n=len(held), ids=ids)
    return "\n".join([t("inbound.unblocked", board=board, id=task_id), tail])


def render_answer(title: str, action: str, held: int, choice=None) -> str:
    """Telegram confirmation of a resolved item: the board and the short action, no ids."""
    lines = [t("reply.resolved", board=title, action=action)]
    if choice is not None:
        label = _clip_to(strip_ids(replace_roles(choice[1])), _OPTION_MAX)
        lines.append(t("reply.choice", letter=choice[0], label=label))
    lines.append(plural("reply.held", held) if held else t("reply.held.none"))
    return "\n".join(lines)


def render_config_alert(kind: str) -> str:
    """A setup alert (``veto``, ``escalation_no_chat``): its own message, never part of a reminder."""
    return "\n".join([t("config.head"), t(f"config.{kind}")])


# -- Telegram delivery: brand prefix, size limit, splitting -----------------------------------
BRAND = "[Kanban - Empowered]"  # the plugin's name: fixed, never translated, so not in the locale catalogs
TELEGRAM_LIMIT = 4096  # sendMessage text limit (UTF-16 code units)


def branded(text: str) -> str:
    """``text`` under the brand line (``[Kanban - Empowered]`` + newline); already branded text is unchanged.

    Every Telegram message the plugin sends or edits goes through this (see ``Coordinator._tg_send`` /
    ``_tg_call`` and ``inbound._reply``). Renderers that split to the limit apply it themselves, because the
    prefix counts toward the 4096 units of every chunk.
    """
    text = str(text)
    line = BRAND + "\n"
    return text if text.startswith(line) else line + text


def _tg_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip_item(text: str, limit: int) -> str:
    """Fit one rendered item in ``limit``: the first and the last line stay, the middle is clipped."""
    if _tg_len(text) <= limit:
        return text
    lines = text.split("\n")
    head, footer, middle = lines[0], lines[-1], "\n".join(lines[1:-1])
    budget = limit - _tg_len(head) - _tg_len(footer) - 2
    if budget < 2:  # pathological: no room for a footer, plain clip
        return _prefix(text, max(0, limit - 1)) + "…"
    return "\n".join([head, _prefix(middle, budget - 1).rstrip() + "…", footer])


def _prefix(text: str, units: int) -> str:
    """The longest prefix of ``text`` that is at most ``units`` UTF-16 units long (binary search)."""
    if _tg_len(text) <= units:
        return text
    lo, hi = 0, len(text)  # invariant: text[:lo] fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tg_len(text[:mid]) <= units:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def render_waits_chunks(
    owner: str,
    dues: list,
    limit: int = TELEGRAM_LIMIT,
    lead: str = "",
    copy: bool = False,
) -> list[tuple[str, list]]:
    """Reminder messages for ``owner``, each within the Telegram limit, as ``(text, dues)`` pairs.

    Dues are packed in order and a due is never split across messages; a single due that alone
    exceeds the limit is clipped (its first and last lines are kept). Every message starts with the
    brand line (counted in the limit), repeats the header (counted over its own items) and numbering restarts at 1, and ``dues`` is that
    message's item list in numbering order, so a reply to a message maps "n — …" to
    ``dues[n - 1]`` of that message and no other. ``lead`` is a line put first in every message
    (after the brand line) and ``copy`` renders the escalated copy (see :func:`render_waits`).
    """
    def render(chunk: list, clip: bool = False) -> str:
        marker = BRAND + "\n" + (lead + "\n" if lead else "")
        text = render_waits(owner, chunk, copy=copy)
        return marker + (_clip_item(text, limit - _tg_len(marker)) if clip else text)

    out: list[tuple[str, list]] = []
    current: list = []
    for due in dues:
        if current and _tg_len(render(current + [due])) > limit:
            out.append((render(current), current))
            current = []
        current = current + [due]
        if len(current) == 1 and _tg_len(render(current)) > limit:
            out.append((render(current, clip=True), current))
            current = []
    if current:
        out.append((render(current), current))
    return out


# -- guard alerts (Telegram) -------------------------------------------------------------
_TITLE_MAX = 100
_URL_MAX = 300
_GUARD_REASONS = ("active_pr", "blocker_auth")


def _guard_reason_text(reason: str) -> str:
    return t(f"guards.reason.{reason}") if reason in _GUARD_REASONS else _clip_to(reason, _TITLE_MAX)


def _clip_to(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _card_line(n: int, alert: dict) -> str:
    title = board_title(alert["board"], alert.get("board_name", ""))
    card = _clip_to(strip_ids(replace_roles(alert.get("title") or "")), _TITLE_MAX)
    head = f"{title} · {card}" if card else title
    return f"{circled(n)} {head}" if n else head


def _guard_item(n: int, alert: dict) -> list[str]:
    lines = [
        _card_line(n, alert),
        t("guards.item.detail", age=natural_age(alert.get("age_s", 0)), reason=_guard_reason_text(alert["reason"]),
          assignee=alert.get("assignee") or t("guards.no_assignee")),
    ]
    if alert.get("pr_url"):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(alert.get("pr_at") or 0))
        lines.append(t("guards.item.pr", url=_clip_to(alert["pr_url"], _URL_MAX), when=when))
    lines.append(t("guards.item.release", id=alert["task_id"]))  # the action itself needs the id
    return lines


def render_guards(alerts: list) -> str:
    """One alert message: a headline, the action, one block per card with its release command."""
    if not alerts:
        return ""
    action = t("guards.action.one") if len(alerts) == 1 else t("guards.action.many", n=len(alerts))
    lines = [t("guards.head"), action]
    many = len(alerts) > 1
    for n, alert in enumerate(alerts, start=1):
        lines.append("")
        lines.extend(_guard_item(n if many else 0, alert))
    return "\n".join(lines)


def render_guard_chunks(alerts: list, limit: int = TELEGRAM_LIMIT) -> list[tuple[str, list]]:
    """Branded alert messages within the Telegram limit as ``(text, alerts)`` pairs; an alert is never split."""
    out: list[tuple[str, list]] = []
    current: list = []
    for alert in alerts:
        if current and _tg_len(branded(render_guards(current + [alert]))) > limit:
            out.append((branded(render_guards(current)), current))
            current = []
        current = current + [alert]
    if current:
        out.append((branded(render_guards(current)), current))
    return [(_prefix(text, limit), items) for text, items in out]


# -- infra recovery alerts (Telegram) ---------------------------------------------------
def undo_comment(n: int, pattern: str) -> str:
    """The card comment that explains why a breaker block was undone."""
    return t("infra.undo_comment.one" if n == 1 else "infra.undo_comment.many", n=n, pattern=pattern)


def render_infra_alert(alert: dict) -> str:
    """One infra alert: ``kind`` "budget" (a card whose undo budget is used up) or "systemic"."""
    if alert.get("kind") == "systemic":
        return "\n".join([
            t("infra.head"),
            t("infra.systemic", n=alert.get("n", 0), window=natural_age(alert.get("window_s", 0))),
        ])
    return "\n".join([
        t("infra.head"),
        t("infra.budget.action"),
        "",
        _card_line(0, alert),
        t("infra.budget.detail", count=alert.get("count", 0), window=natural_age(alert.get("window_s", 0))),
        t("infra.budget.how", board=alert["board"], id=alert["task_id"]),  # the action itself needs the id
    ])
