"""The single adapter between the plugin and Hermes.

This is the ONLY module in ``ke/`` allowed to import ``hermes_cli.*`` (or other
Hermes internals). Everything else talks to Hermes through these functions, so a
Hermes upgrade only ever needs fixes here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_boards as _kbb
from hermes_cli import kanban_db_connect as _kbc

# Hermes revision this adapter was written and verified against.
HERMES_COMPAT = "bf0203a794"

COMMENT_AUTHOR = "kanban-empowered"
_COMMENT_PREFIX = COMMENT_AUTHOR + ": "

_VETO_HOOKS = frozenset({"kanban_pre_claim", "kanban_pre_complete"})


# -- boards / connections ----------------------------------------------------
def boards() -> list[str]:
    """Slugs of all non-archived boards (``default`` first)."""
    return [b["slug"] for b in _kbb.list_boards(include_archived=False)]


def connect(board: str) -> sqlite3.Connection:
    """Open the kanban DB of ``board`` (the caller closes it)."""
    return _kbc.connect(board=board)


# -- reads -------------------------------------------------------------------
def task(conn: sqlite3.Connection, tid: str):
    return _kb.get_task(conn, tid)


def events(conn: sqlite3.Connection, tid: str) -> list:
    return _kb.list_events(conn, tid)


def comments(conn: sqlite3.Connection, tid: str) -> list:
    return _kb.list_comments(conn, tid)


def children(conn: sqlite3.Connection, tid: str) -> list[str]:
    return list(_kb.child_ids(conn, tid))


def parents(conn: sqlite3.Connection, tid: str) -> list[str]:
    return list(_kb.parent_ids(conn, tid))


# -- writes ------------------------------------------------------------------
def unblock(conn: sqlite3.Connection, tid: str) -> bool:
    return bool(_kb.unblock_task(conn, tid))


def comment(conn: sqlite3.Connection, tid: str, body: str) -> int:
    """Comment as ``kanban-empowered``; the body is prefixed unless it already is."""
    if not body.startswith(_COMMENT_PREFIX):
        body = _COMMENT_PREFIX + body
    return int(_kb.add_comment(conn, tid, COMMENT_AUTHOR, body))


def assign(conn: sqlite3.Connection, tid: str, profile: str) -> bool:
    return bool(_kb.assign_task(conn, tid, profile))


# -- environment -------------------------------------------------------------
def profiles() -> list[str]:
    """Profile names: sub-directories of ``<hermes root>/profiles`` plus ``default``."""
    from hermes_constants import get_default_hermes_root

    names = {"default"}
    pdir = Path(get_default_hermes_root()) / "profiles"
    if pdir.is_dir():
        for child in pdir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                names.add(child.name)
    return ["default"] + sorted(names - {"default"})


def veto_hooks_available() -> bool:
    """True when this Hermes build exposes the kanban veto hooks (checked at call time)."""
    import hermes_cli.plugins as plugins

    return _VETO_HOOKS <= set(getattr(plugins, "VALID_HOOKS", ()))
