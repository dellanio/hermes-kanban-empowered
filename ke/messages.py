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

from . import hermes_api

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
    # waits (human reminders)
    "waits.head.reminder": "🟠 Needs you",
    "waits.head.urgent": "🔴 Escalated: needs you",
    "waits.head.digest": "🔴 Daily summary: still needs you",
    "waits.single.line": "{head} · {board} · stopped for {age}",
    "waits.group.line": "{head} · {n} items for {owner}",
    "waits.item.line": "{n}. {board} · {id} · stopped for {age}",
    "waits.need": "What I need: {text}",
    "waits.why": "Why: {text}",
    "waits.no_reason": "(no reason given)",
    "waits.held.one": "What is held behind: 1 card ({ids}).",
    "waits.held.many": "What is held behind: {n} cards ({ids}).",
    "waits.how.single": "How to reply: reply \"done\" to this message or run: hermes kanban-empowered done {id}",
    "waits.how.group": "How to reply: reply \"{n} done\" to this message or run: hermes kanban-empowered done <id>",
    "waits.copy_of": "Escalated copy: this was sent to {owner}.",
    "waits.urgent": "🚨 URGENT: no escalation contact is configured, so only you received this.",
    # inbound replies (Telegram) and the done command
    "inbound.which_item": 'Which item? Reply "<n> done" with a number from 1 to {n}.',
    "inbound.changed": "This request has changed since that message; nothing was changed.",
    "inbound.expired": "This request has expired; nothing was changed.",
    "inbound.error": "Something went wrong while processing this reply; check the card with: hermes kanban-empowered explain {id}",
    "inbound.unblocked": "✅ Unblocked {board} · {id}.",
    "inbound.held.none": "Nothing else is waiting on it.",
    "inbound.held.one": "Next in line behind it: 1 card ({ids}).",
    "inbound.held.many": "Next in line behind it: {n} cards ({ids}).",
    "inbound.comment.telegram": "unblocked via Telegram by {name} ({user_id}): {text}",
    "inbound.comment.cli": "unblocked via CLI by {user}",
    "done.no_wait": "card {card} on board {board} has no open human wait; nothing was changed",
    # setup warnings
    "veto.warning": "kanban-empowered: this Hermes build lacks the kanban veto hooks (kanban_pre_claim / kanban_pre_complete). Guards run in cooperative-only mode. Run: hermes kanban-empowered doctor",
}


def t(key: str, **kw) -> str:
    """Text for ``key`` in the user's Hermes language, English when Hermes has none.

    ``kw`` are format arguments; they must not be named ``lang`` or ``key`` (``agent.i18n.t``
    takes ``lang`` itself, and ``key`` is this function's own parameter).
    """
    out = hermes_api.translate(PREFIX + key, **kw)
    if out is not None:
        return out
    text = EN.get(key)
    if text is None:
        return key
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


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


# -- human wait reminders ---------------------------------------------------------
_RAW_REASON_MAX = 600
_HELD_IDS_SHOWN = 5


def _wait_head(stage: str) -> str:
    if stage == "digest":
        return t("waits.head.digest")
    if stage == "escalation":
        return t("waits.head.urgent")
    return t("waits.head.reminder")


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _RAW_REASON_MAX else text[: _RAW_REASON_MAX - 1] + "…"


def _wait_body(due) -> list[str]:
    """What I need / why / what is held behind, for one wait."""
    lines = []
    if due.ask is not None:
        lines.append(t("waits.need", text=due.ask.need))
        lines.append(t("waits.why", text=due.ask.why))
    else:  # blocked before the structured ask existed: the raw reason is all there is
        lines.append(t("waits.need", text=_clip(due.reason) if due.reason.strip() else t("waits.no_reason")))
    held = list(due.held_behind)
    if held:
        shown = held[:_HELD_IDS_SHOWN]
        ids = ", ".join(shown) + (", …" if len(held) > len(shown) else "")
        lines.append(t("waits.held.one", ids=ids) if len(held) == 1 else t("waits.held.many", n=len(held), ids=ids))
    return lines


def render_waits(owner: str, dues: list) -> str:
    """The reminder message for ``owner``: one item as-is, several numbered and grouped."""
    if not dues:
        return ""
    if len(dues) == 1:
        due = dues[0]
        head = t(
            "waits.single.line",
            head=_wait_head(due.stage),
            board=due.board,
            age=human_age(due.age_s),
        )
        return "\n".join([head, *_wait_body(due), t("waits.how.single", id=due.task_id)])
    urgent = any(d.stage in ("escalation", "digest") for d in dues)
    lead = _wait_head("digest" if any(d.stage == "digest" for d in dues) else "escalation" if urgent else "reminder1")
    lines = [t("waits.group.line", head=lead, n=len(dues), owner=owner)]
    for n, due in enumerate(dues, start=1):
        lines.append(t("waits.item.line", n=n, board=due.board, id=due.task_id, age=human_age(due.age_s)))
        lines.extend("   " + body for body in _wait_body(due))
    lines.append(t("waits.how.group", n=1))
    return "\n".join(lines)


def render_done(board: str, task_id: str, held: list) -> str:
    """Confirmation after a human resolved a wait: the card and what is next in line behind it."""
    held = list(held)
    if not held:
        tail = t("inbound.held.none")
    else:
        shown = held[:_HELD_IDS_SHOWN]
        ids = ", ".join(shown) + (", …" if len(held) > len(shown) else "")
        tail = t("inbound.held.one", ids=ids) if len(held) == 1 else t("inbound.held.many", n=len(held), ids=ids)
    return "\n".join([t("inbound.unblocked", board=board, id=task_id), tail])


# -- Telegram delivery: size limit, splitting, urgent marker ------------------------------
TELEGRAM_LIMIT = 4096  # sendMessage text limit (UTF-16 code units)


def _tg_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip_item(text: str, limit: int) -> str:
    """Fit one rendered item in ``limit``: the head and the reply footer stay, the middle is clipped."""
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
    urgent_escalation: bool = False,
    limit: int = TELEGRAM_LIMIT,
    lead: str = "",
) -> list[tuple[str, list]]:
    """Reminder messages for ``owner``, each within the Telegram limit, as ``(text, dues)`` pairs.

    Dues are packed in order and a due is never split across messages; a single due that alone
    exceeds the limit is clipped (its head and reply footer are kept). Numbering restarts at 1
    in every message and ``dues`` is that message's item list, in numbering order, so a reply
    to a message maps "n done" to ``dues[n - 1]`` of that message and no other.
    ``urgent_escalation`` marks the messages that carry an escalation (nobody else will hear of
    it); ``lead`` is a line put first in every message (e.g. who an escalated copy is from).
    """
    def render(chunk: list, clip: bool = False) -> str:
        urgent = urgent_escalation and any(d.stage == "escalation" for d in chunk)
        marker = (lead + "\n" if lead else "") + (t("waits.urgent") + "\n" if urgent else "")
        text = render_waits(owner, chunk)
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
