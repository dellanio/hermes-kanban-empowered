"""``pre_tool_call`` guard: humans-waiting blocks must carry a structured ask.

Hermes treats a raising ``pre_tool_call`` callback as a veto (fail-closed), so
this never raises: unexpected input is read as "missing" and only a
``kanban_block`` that waits on a human can be vetoed.
"""
from __future__ import annotations

from .asks import ASK_TEMPLATE, parse_ask

_BLOCK_MESSAGE = (
    "kanban-empowered: bloqueio esperando pessoa precisa do pedido estruturado.\n" + ASK_TEMPLATE
)


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
    return {"action": "block", "message": _BLOCK_MESSAGE}
