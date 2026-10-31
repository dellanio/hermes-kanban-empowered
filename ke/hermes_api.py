"""The single adapter between the plugin and Hermes.

This is the ONLY module in ``ke/`` allowed to import ``hermes_cli.*`` (or other
Hermes internals). Everything else talks to Hermes through these functions, so a
Hermes upgrade only ever needs fixes here.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator

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


@contextlib.contextmanager
def connect(board: str) -> Iterator[sqlite3.Connection]:
    """Context manager yielding the kanban DB connection of ``board``.

    The connection is always closed on exit (``connect_closing``): a bare
    ``with sqlite3_conn`` only commits and would leak fds in a long-running
    process (Hermes incident #33159).
    """
    with _kbc.connect_closing(board=board) as conn:
        yield conn


# -- reads -------------------------------------------------------------------
def task(conn: sqlite3.Connection, tid: str):
    return _kb.get_task(conn, tid)


def events(conn: sqlite3.Connection, tid: str) -> list:
    return _kb.list_events(conn, tid)


def comments(conn: sqlite3.Connection, tid: str) -> list:
    return _kb.list_comments(conn, tid)


def runs(conn: sqlite3.Connection, tid: str) -> list:
    """Runs of ``tid`` in start order (active and closed)."""
    return _kb.list_runs(conn, tid)


def latest_summary_run(conn: sqlite3.Connection, tid: str):
    """The run holding the newest non-empty summary, or ``None``.

    Same selection as Hermes' ``latest_summary``/``latest_summaries`` (newest by
    ``COALESCE(ended_at, started_at)`` then id), but returning the whole run so the
    caller also gets the timestamp and run id. Built on the public ``list_runs``.
    """
    best = None
    best_key = None
    for run in _kb.list_runs(conn, tid):
        if not run.summary:
            continue
        key = (run.ended_at if run.ended_at is not None else run.started_at, run.id)
        if best_key is None or key >= best_key:
            best, best_key = run, key
    return best


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list:
    """Non-archived tasks, optionally filtered by ``status``."""
    return _kb.list_tasks(conn, status=status)


def children(conn: sqlite3.Connection, tid: str) -> list[str]:
    return list(_kb.child_ids(conn, tid))


def parents(conn: sqlite3.Connection, tid: str) -> list[str]:
    return list(_kb.parent_ids(conn, tid))


# -- writes ------------------------------------------------------------------
def unblock(conn: sqlite3.Connection, tid: str) -> bool:
    return bool(_kb.unblock_task(conn, tid))


def comment(conn: sqlite3.Connection, tid: str, body: str) -> int:
    """Comment as ``kanban-empowered``; the body is stripped and prefixed unless it already is.

    Raises ValueError on an empty/whitespace-only body.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body is required")
    if body.startswith(COMMENT_AUTHOR + ":"):
        body = body[len(COMMENT_AUTHOR) + 1:].strip()
        if not body:
            raise ValueError("comment body is required")
    body = _COMMENT_PREFIX + body
    return int(_kb.add_comment(conn, tid, COMMENT_AUTHOR, body))


def assign(conn: sqlite3.Connection, tid: str, profile: str) -> bool:
    """Assign ``tid`` to ``profile``; False if the task is unknown.

    Raises RuntimeError (from Hermes) while the task is running under a claim.
    """
    return bool(_kb.assign_task(conn, tid, profile))


# -- i18n --------------------------------------------------------------------
def translate(full_key: str, **kw) -> str | None:
    """Hermes' translation of ``full_key`` (``agent.i18n.t``), or ``None`` when it has none.

    Hermes returns the bare key on a miss, so "the key came back" means untranslated; an
    import or lookup failure also yields ``None`` and the caller falls back to its own text.
    """
    try:
        from agent.i18n import t as _t

        out = _t(full_key, **kw)
    except Exception:
        return None
    return None if not isinstance(out, str) or out == full_key else out


# -- environment -------------------------------------------------------------
def hermes_home() -> Path:
    """The active Hermes home directory."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


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


def active_profile() -> str:
    """Name of the profile this process runs as (``default``, a profile name, or ``custom``)."""
    from hermes_cli.profiles import get_active_profile_name

    return str(get_active_profile_name())


def hermes_head() -> str | None:
    """Short git HEAD of the installed Hermes checkout, or ``None`` when it cannot be read."""
    import subprocess

    import hermes_cli

    root = Path(hermes_cli.__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def profiles_with_plugin(plugin: str = "kanban-empowered") -> list[str]:
    """Profiles whose ``config.yaml`` has ``plugins.entries.<plugin>`` (``default`` first)."""
    from hermes_constants import get_default_hermes_root

    try:
        import yaml
    except ImportError:
        return []

    root = Path(get_default_hermes_root())
    found: list[str] = []
    for name in profiles():
        cfg = (root / "config.yaml") if name == "default" else (root / "profiles" / name / "config.yaml")
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries = ((data or {}).get("plugins") or {}).get("entries") if isinstance(data, dict) else None
        if isinstance(entries, dict) and plugin in entries:
            found.append(name)
    return found
