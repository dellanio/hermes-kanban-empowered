"""Structured human ask: ``Need:`` / ``Why:`` / ``How to reply:`` (also ``Preciso:`` / ``Por quê:`` /
``Como responder:``; ``Reply:`` is accepted as a short label)."""
from __future__ import annotations

import re
from dataclasses import dataclass


def ask_template() -> str:
    """The three-line template shown to an agent, in the user's language (``messages.t``)."""
    from .messages import t

    return t("guard.template")


@dataclass(frozen=True)
class Ask:
    need: str
    why: str
    reply: str


_LABELS = {
    "need": ("preciso", "need"),
    "why": ("por quê", "por que", "porque", "why"),
    "reply": ("como responder", "how to reply", "reply"),
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
