"""Structured human ask: ``Preciso:`` / ``Por quê:`` / ``Como responder:``."""
from __future__ import annotations

import re
from dataclasses import dataclass

ASK_TEMPLATE = (
    "Use exatamente estas três linhas no motivo do bloqueio:\n"
    "Preciso: <o que você precisa que a pessoa faça ou informe>\n"
    "Por quê: <por que o trabalho não avança sem isso>\n"
    "Como responder: <como a pessoa deve responder, ex.: responda 'feito'>"
)


@dataclass(frozen=True)
class Ask:
    need: str
    why: str
    reply: str


_LABELS = {
    "need": ("preciso", "need"),
    "why": ("por quê", "por que", "porque", "why"),
    "reply": ("como responder", "reply"),
}
_LINE_RE = re.compile(
    r"^\s*(?P<label>"
    + "|".join(re.escape(l) for labels in _LABELS.values() for l in labels)
    + r")\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_FIELD_OF = {label: field for field, labels in _LABELS.items() for label in labels}


def parse_ask(reason) -> Ask | None:
    """Return the structured ask, or ``None`` if any of the three fields is missing/empty."""
    if not isinstance(reason, str):
        return None
    found: dict[str, str] = {}
    for line in reason.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        field = _FIELD_OF[m.group("label").lower()]
        value = m.group("value")
        if value and field not in found:
            found[field] = value
    if len(found) != 3:
        return None
    return Ask(need=found["need"], why=found["why"], reply=found["reply"])
