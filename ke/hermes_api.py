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


def board_exists(board: str) -> bool:
    """True when ``board`` is a live (non-archived) board, ``default`` included."""
    return bool(board) and board in boards()


def task_exists(board: str, tid: str, live=None) -> bool:
    """True when card ``tid`` is on the live board ``board`` (False for an unknown or archived board, which
    is never opened, so a lookup can never create a board). ``live`` is the caller's list of live boards
    (:func:`boards`), so a scan over several boards lists them once; ``None`` lists them here."""
    if not board or board not in (boards() if live is None else live):
        return False
    with connect(board) as conn:
        return _kb.get_task(conn, tid) is not None


def claimable_task(board: str, tid: str, assignee=None, live=None) -> bool:
    """True when ``tid`` is a ``ready`` card of the live board ``board`` and, when ``assignee`` is given, is
    assigned to it: the card a ``kanban_pre_claim`` call is about (ids are only unique per board, so a card
    of another board with the same id does not match). Never opens an unknown or archived board."""
    if not board or board not in (boards() if live is None else live):
        return False
    with connect(board) as conn:
        task = _kb.get_task(conn, tid)
    if task is None or task.status != "ready":
        return False
    return assignee is None or task.assignee == assignee


@contextlib.contextmanager
def connect(board: str) -> Iterator[sqlite3.Connection]:
    """Context manager yielding the kanban DB connection of ``board``.

    The connection is always closed on exit (``connect_closing``): a bare
    ``with sqlite3_conn`` only commits and would leak fds in a long-running
    process (Hermes incident #33159).
    """
    with _kbc.connect_closing(board=board) as conn:
        yield conn


def board_name(board: str) -> str:
    """The board's display name (``board.json`` ``name``; Hermes derives one from the slug)."""
    try:
        name = _kbb.read_board_metadata(board).get("name")
    except Exception:
        return board
    return str(name).strip() if name else board


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
def translate(full_key: str, lang: str | None = None, **kw) -> str | None:
    """Hermes' translation of ``full_key`` (``agent.i18n.t``), or ``None`` when it has none.

    ``lang`` forces a language (the plugin's ``language`` setting); ``None`` follows the user's
    Hermes language. Hermes returns the bare key on a miss, so "the key came back" means
    untranslated; an import or lookup failure also yields ``None`` and the caller falls back to
    its own text.
    """
    try:
        from agent.i18n import t as _t

        out = _t(full_key, lang=lang, **kw) if lang else _t(full_key, **kw)
    except Exception:
        return None
    return None if not isinstance(out, str) or out == full_key else out


def resolve_language(value: str) -> str | None:
    """Hermes' canonical id for a language value (``agent.i18n.resolve_language_id``), ``None`` when
    Hermes does not support it. Raises when Hermes cannot be asked (the caller keeps the value)."""
    from agent.i18n import resolve_language_id

    return resolve_language_id(value)


_LOCALE_SENTINEL = "kanban_empowered.waits.header"
_dashboard_packs: list = []


def ensure_locales(locales_dir: Path) -> bool:
    """Register the plugin's locale catalogs with Hermes when no one did (the dashboard backend can run
    in a process where the agent half's ``provides_locales`` was not loaded). Returns True when it
    registered them; a no-op when Hermes already translates the plugin's keys. Never raises."""
    if translate(_LOCALE_SENTINEL, lang="en") is not None or _dashboard_packs:
        return False
    try:
        from agent.i18n_layers import parse_locale_file, register_pack

        for file in sorted(Path(locales_dir).glob("*.yaml")):
            lang = file.stem
            if "." in lang:  # <lang>.tui.yaml / <lang>.desktop.yaml: not the core surface
                continue
            _dashboard_packs.append(
                register_pack(lang, "core", parse_locale_file(file), source="plugin:kanban-empowered:dashboard")
            )
    except Exception:
        return False
    return bool(_dashboard_packs)


def release_locales() -> None:
    """Drop the catalogs :func:`ensure_locales` registered (tests, reloads)."""
    try:
        from agent.i18n_layers import unregister_pack
    except Exception:
        _dashboard_packs.clear()
        return
    while _dashboard_packs:
        try:
            unregister_pack(_dashboard_packs.pop())
        except Exception:
            pass


def plugin_settings(plugin: str = "kanban-empowered") -> dict:
    """A deep copy of ``plugins.entries.<plugin>.settings`` from the active profile's config (``{}`` when
    unset or unreadable): what ``ctx.get_config`` reads, for code that has no plugin ``ctx`` (the dashboard)."""
    import copy

    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    node = cfg
    for part in ("plugins", "entries", plugin, "settings"):
        node = node.get(part) if isinstance(node, dict) else None
    return copy.deepcopy(node) if isinstance(node, dict) else {}


def language() -> str | None:
    """The user's Hermes language id (``agent.i18n.get_language``), ``None`` when it cannot be read."""
    try:
        from agent.i18n import get_language

        return get_language()
    except Exception:
        return None


# -- Telegram network --------------------------------------------------------
class TelegramNet:
    """Hermes' Telegram network helpers behind plain, synchronous methods.

    Wraps ``plugins.platforms.telegram.telegram_network`` (the module Hermes' own Telegram adapter uses to
    survive a broken path to ``api.telegram.org``): fallback-IP parsing and discovery, the seed IPv4 list,
    proxy resolution, TCP keepalive options and the "retryable connect error" test. Each method looks its
    Hermes function up at call time; ``keepalive``, ``discover`` and ``proxy_url`` are fail-soft here, and the
    consumer (``ke.telegram.TelegramTransport``) also guards every call, so a helper renamed or moved by a
    Hermes upgrade can disable at most that one feature, never the send.
    """

    def __init__(self, module) -> None:
        self._tn = module
        self.seed_ips: list[str] = list(getattr(module, "SEED_FALLBACK_IPS", None) or ("149.154.166.110", "149.154.167.220"))

    def parse_ips(self, value: str | None) -> list[str]:
        """Valid public IPv4 literals out of a comma-separated list (Hermes' ``TELEGRAM_FALLBACK_IPS`` rules)."""
        return list(self._tn.parse_fallback_ip_env(value))

    def discover(self, timeout_s: float) -> list[str] | None:
        """Fallback IPs via Hermes' DNS-over-HTTPS discovery, or ``None`` on failure / after ``timeout_s``.

        Hermes' discovery is a coroutine; it runs on a private event loop in a helper thread, so this is safe
        to call from the coordinator's worker thread and even from inside a running loop.
        """
        import asyncio
        import threading

        box: list = []

        def run() -> None:
            try:
                box.append(asyncio.run(self._tn.discover_fallback_ips()))
            except Exception:
                pass

        worker = threading.Thread(target=run, name="ke-telegram-discover", daemon=True)
        worker.start()
        worker.join(max(0.0, timeout_s))
        return list(box[0]) if box and box[0] else None

    def proxy_url(self, hosts) -> str | None:
        """The proxy Hermes would use for ``hosts`` (``TELEGRAM_PROXY`` / system proxy), ``None`` when none."""
        try:
            return self._tn._resolve_proxy_url(target_hosts=list(hosts))
        except Exception:
            return None

    def keepalive(self) -> list:
        """``socket_options`` for httpx: SO_KEEPALIVE plus the idle/interval/count knobs the OS exposes."""
        try:
            return list(self._tn.tcp_keepalive_socket_options())
        except Exception:
            import socket

            return [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]

    def retryable(self, exc: Exception) -> bool:
        """Hermes' test for a connect-phase failure worth retrying on another path."""
        return bool(self._tn._is_retryable_connect_error(exc))

    @property
    def verify(self):
        """The platform trust store Hermes verifies Telegram TLS with (``True`` if it cannot be built)."""
        try:
            from agent.ssl_verify import platform_ssl_context

            return platform_ssl_context()
        except Exception:
            return True


def telegram_net() -> TelegramNet:
    """The Hermes Telegram network helpers; raises (ImportError, AttributeError...) when Hermes has none."""
    from plugins.platforms.telegram import telegram_network as module

    return TelegramNet(module)


# -- environment -------------------------------------------------------------
def hermes_home() -> Path:
    """The active Hermes home directory."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def profile_home(name: str) -> Path:
    """The Hermes home of profile ``name``: the root for ``default``, ``<root>/profiles/<name>`` otherwise.

    Hermes' own resolver (``hermes_cli.profiles.get_profile_dir``), anchored to the Hermes root and not
    to the active profile, so every profile process computes the same path for the same name.
    """
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(name))


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


def _git_dir(root: Path) -> Path | None:
    """The real git directory of a checkout: ``.git`` itself, or the target of a ``gitdir:`` file."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    return target if target.is_absolute() else (root / target).resolve()


def _read_head_sha(git_dir: Path) -> str | None:
    """Full sha ``HEAD`` points at (loose ref, then packed-refs; detached HEAD is the sha itself)."""
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head[len("ref:"):].strip()
    # Worktrees keep per-worktree HEAD in git_dir but shared refs in the "commondir".
    dirs = [git_dir]
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        dirs.append((git_dir / common).resolve())
    except OSError:
        pass
    for d in dirs:
        try:
            sha = (d / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if sha:
            return sha
    for d in dirs:
        try:
            lines = (d / "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and not line.startswith(("#", "^")):
                return parts[0]
    return None


def hermes_head() -> str | None:
    """Short (10-char) git HEAD of the installed Hermes checkout, or ``None`` when it cannot be read.

    Pure file reads (no git subprocess): ``.git/HEAD`` -> loose ref -> ``packed-refs``.
    """
    import hermes_cli

    root = Path(hermes_cli.__file__).resolve().parent.parent
    try:
        git_dir = _git_dir(root)
        sha = _read_head_sha(git_dir) if git_dir is not None else None
    except Exception:
        return None
    return sha[:10] if sha else None


def _names(value) -> list:
    """``value`` as a list of names (a bare string or anything else that is not a list counts as empty)."""
    return list(value) if isinstance(value, (list, tuple)) else []


def profiles_with_plugin(plugin: str = "kanban-empowered") -> list[str]:
    """Profiles that have ``plugin`` installed and enabled (``default`` first).

    A profile counts when ``<profile home>/plugins/<plugin>`` exists and its ``config.yaml`` either lists
    the plugin in ``plugins.enabled`` (and not in ``plugins.disabled``; this is how Hermes records
    enablement) or has a ``plugins.entries.<plugin>`` section.
    """
    try:
        from hermes_yaml import safe_load  # Hermes' own loader (PyYAML is not a declared dependency)
    except ImportError:
        return []

    found: list[str] = []
    for name in profiles():
        try:
            home = profile_home(name)
            if not (home / "plugins" / plugin).is_dir():
                continue
            data = safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        except Exception:
            continue
        section = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(section, dict):
            continue
        entries = section.get("entries")
        listed = plugin in _names(section.get("enabled")) and plugin not in _names(section.get("disabled"))
        if listed or (isinstance(entries, dict) and plugin in entries):
            found.append(name)
    return found
