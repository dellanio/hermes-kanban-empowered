"""``pre_tool_call`` guard: humans-waiting blocks must carry a structured ask.

Hermes treats a raising ``pre_tool_call`` callback as a veto (fail-closed), so
this never raises: unexpected input is read as "missing" and only a
``kanban_block`` that waits on a human can be vetoed.
"""
from __future__ import annotations

from .asks import ask_template, parse_ask

# Used only if the i18n layer itself fails: the guard must always explain what it wants.
_FALLBACK_MESSAGE = (
    "kanban-empowered: a block that waits on a person needs a structured ask.\n"
    "Use these lines in the block reason; the first three are required:\n"
    "Need: <the action you need from the person, at most two sentences, no card ids>\n"
    "Why: <why the work cannot move without it, one short sentence>\n"
    "How to reply: <the word the person should reply, e.g. reply \"done\">\n"
    "Subject: <optional: a short topic shown next to the board name>\n"
    "Next step: <optional: what you will do once answered>\n"
    "Options: <optional: one choice per line below>\n"
    "A — <first choice>\n"
    "B — <second choice>"
)


def _block_message() -> str:
    """The veto text for the agent, in the user's language (English by default)."""
    try:
        from .messages import t

        return t("guard.block", template=ask_template())
    except Exception:
        return _FALLBACK_MESSAGE


def pre_tool_call(tool_name, args=None, settings=None, **kw):
    if tool_name != "kanban_block":
        return None
    if settings is None:
        from .settings import Settings

        settings = Settings.from_mapping({})
    if not settings.require_structured_ask:
        return None
    if not isinstance(args, dict):
        args = {}
    kind = args.get("kind")
    try:
        waits_on_human = kind is None or kind in settings.human_kinds
    except Exception:
        waits_on_human = False
    if not waits_on_human:
        return None
    if parse_ask(args.get("reason")) is not None:
        return None
    return {"action": "block", "message": _block_message()}
