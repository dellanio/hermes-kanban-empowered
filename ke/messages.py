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
