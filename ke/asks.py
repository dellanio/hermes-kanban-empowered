"""Structured human ask: ``Need:`` / ``Why:`` / ``How to reply:`` (also ``Preciso:`` / ``Por quê:`` /
``Como responder:``; ``Reply:`` is accepted as a short label).

Optional fields: ``Subject:`` / ``Assunto:`` (a short topic shown next to the board name),
``Next step:`` / ``Próximo passo:`` and ``Options:`` / ``Opções:`` followed by one line per choice
(``A — text``, ``A) text``, ``A: text`` or ``A - text``, letters A–Z). Asks written before the
optional fields existed parse exactly as before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def ask_template() -> str:
    """The template shown to an agent, in the user's language (``messages.t``)."""
    from .messages import t

    return t("guard.template")


@dataclass(frozen=True)
class Ask:
    need: str
    why: str
    reply: str
    options: tuple = ()  # ((letter, label), ...) in the order written; letters upper-case and unique
    subject: str = ""
    next_step: str = ""

    def option(self, letter: str):
        """The label of option ``letter`` (any case), or ``None`` when the ask does not declare it."""
        letter = (letter or "").strip().upper()
        for key, label in self.options:
            if key == letter:
                return label
        return None


_REQUIRED = ("need", "why", "reply")
_LABELS = {
    "need": ("preciso", "need"),
    "why": ("por quê", "por que", "porque", "why"),
    "reply": ("como responder", "how to reply", "reply"),
    "subject": ("assunto", "subject"),
    "next_step": ("próximo passo", "proximo passo", "next step"),
    "options": ("opções", "opcoes", "options"),
}
_LINE_RE = re.compile(
    r"^\s*(?P<label>"
    + "|".join(re.escape(l) for labels in _LABELS.values() for l in labels)
    + r")\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_FIELD_OF = {label: field for field, labels in _LABELS.items() for label in labels}
# "A — text", "A – text", "A - text", "A) text", "A: text" (one letter, a separator, the label)
OPTION_RE = re.compile(r"^\s*(?P<letter>[A-Za-z])\s*[—–\-:)]\s*(?P<label>\S.*?)\s*$")


def parse_ask(reason) -> Ask | None:
    """Return the structured ask, or ``None`` if any of the three required fields is missing/empty."""
    if not isinstance(reason, str):
        return None
    found: dict[str, str] = {}
    options: list[tuple[str, str]] = []
    in_options = False
    seen_options = False
    for line in reason.splitlines():
        m = _LINE_RE.match(line)
        if m:
            field = _FIELD_OF[m.group("label").lower()]
            value = m.group("value")
            in_options = False
            if field == "options":
                if not seen_options:  # only the first option list counts
                    seen_options = in_options = True
                    first = OPTION_RE.match(value) if value else None
                    if first:
                        _add_option(options, first)
                continue
            if value and field not in found:
                found[field] = value
            continue
        if in_options:
            if not line.strip():
                continue
            opt = OPTION_RE.match(line)
            if opt:
                _add_option(options, opt)
                continue
            in_options = False  # any other line ends the option list
    if any(not found.get(f) for f in _REQUIRED):
        return None
    return Ask(
        need=found["need"],
        why=found["why"],
        reply=found["reply"],
        options=tuple(options),
        subject=found.get("subject", ""),
        next_step=found.get("next_step", ""),
    )


def _add_option(options: list, m) -> None:
    letter = m.group("letter").upper()
    if any(key == letter for key, _ in options):
        return  # the first definition of a letter wins
    options.append((letter, m.group("label")))


# -- reply words ------------------------------------------------------------------
# Confirmation words accepted for every item (pt and en), besides the item's own reply words.
CONFIRM_WORDS = frozenset({
    "feito", "conectei", "validado", "liguei", "ok", "aprovado", "pronto",
    "done", "connected", "validated", "approved",
})
# Leading instruction verbs in a "How to reply" text ("responda feito", "reply done"): not the word.
_INSTRUCTION = re.compile(
    r"^\s*(?:responda|responder|responde|reply|answer|diga|say|digite|type|escreva|write|envie|send)\b\s*:?\s*",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“”‘’«»]([^\"“”‘’«»]+)[\"“”‘’«»]")
_SPLIT = re.compile(r"\s*(?:/|,|;|\bou\b|\bor\b)\s*", re.IGNORECASE)
_WORD = re.compile(r"^[\w\-]+$", re.UNICODE)
_STRIP = "\"“”'‘’«».!?:;()[]{} "


def reply_words(ask: Ask | None) -> list[str]:
    """Words that confirm this ask, primary first (lower-case, unique).

    The primary word is the first quoted single word of the reply text, else its first single-word
    part; further words come from the parts split by "/", ",", " ou ", " or ". Instruction verbs in
    front ("responda", "reply", ...) are not words.
    """
    if ask is None or not isinstance(ask.reply, str):
        return []
    text = ask.reply
    words: list[str] = []

    def add(w: str) -> None:
        w = w.strip(_STRIP).lower()
        if w and _WORD.match(w) and w not in words:
            words.append(w)

    for q in _QUOTED.findall(text):
        if len(q.split()) == 1:
            add(q)
            break
    for part in _SPLIT.split(text):
        part = _INSTRUCTION.sub("", part.strip())
        tokens = [tok for tok in part.split() if tok.strip(_STRIP)]
        if len(tokens) == 1:
            add(tokens[0])
    if not words:  # nothing single-word: the first word after the verb, if it is a plausible reply
        tokens = _INSTRUCTION.sub("", text.strip()).split()
        if tokens and tokens[0].strip(_STRIP).lower() in CONFIRM_WORDS:
            add(tokens[0])
    return words


def primary_word(ask: Ask | None) -> str:
    """The ask's primary reply word, ``""`` when it has none."""
    words = reply_words(ask)
    return words[0] if words else ""


def reply_is_plain(ask: Ask | None) -> bool:
    """True when the reply text says nothing beyond its primary word (``responda "conectei"``)."""
    word = primary_word(ask)
    if not word:
        return False
    rest = _INSTRUCTION.sub("", ask.reply.strip())
    tokens = [tok.strip(_STRIP).lower() for tok in rest.split() if tok.strip(_STRIP)]
    return tokens == [word]
