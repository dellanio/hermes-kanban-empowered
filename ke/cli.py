"""``hermes kanban-empowered ...`` command line."""
from __future__ import annotations

import time
from typing import Any, Optional

from . import conditions, hermes_api
from .leader import LeaderLock
from .messages import t
from .settings import Settings
from .store import Store

_settings: Optional[Settings] = None
_config_error: Optional[str] = None


def bind(settings: Settings, config_error: Optional[str] = None) -> None:
    """Give the CLI the settings ``register(ctx)`` built (Hermes calls handlers with ``args`` only).

    ``config_error`` is set when the user's config was invalid and defaults were used instead.
    """
    global _settings, _config_error
    _settings = settings
    _config_error = config_error


def setup(parser: Any) -> None:
    """Build the ``kanban-empowered`` subcommand tree on the argparse parser Hermes hands over."""
    sub = parser.add_subparsers(dest="ke_cmd")
    sub.add_parser("doctor", help="Show plugin/Hermes compatibility and coordinator status")
    ex = sub.add_parser("explain", help="Explain why a card is (not) moving")
    ex.add_argument("card", help="card id (e.g. t_1a2b3c4d)")
    ex.add_argument("--board", default=None, help="board slug (default: search all boards)")
    sub.add_parser("status", help="Show what is running, waiting and resolved across boards")
    parser.set_defaults(ke_parser=parser)


def run(args: Any) -> int:
    """Dispatch a parsed ``kanban-empowered`` command."""
    settings = _settings if _settings is not None else Settings.from_mapping({})
    cmd = getattr(args, "ke_cmd", None)
    if cmd == "doctor":
        run_doctor(settings, config_error=_config_error)
        return 0
    if cmd == "explain":
        return run_explain(settings, args.card, getattr(args, "board", None))
    if cmd == "status":
        return run_status(settings)
    parser = getattr(args, "ke_parser", None)
    if parser is not None:
        parser.print_help()
    else:
        print("usage: hermes kanban-empowered {doctor,explain,status}")
    return 0 if cmd is None else 2


def _open_store(settings: Settings) -> Store:
    return Store(settings.shared_state_dir / "state.db")


def run_explain(settings: Settings, card: str, board: Optional[str]) -> int:
    """Print the conditions of ``card`` (read-only on the Hermes boards). Exit 1 if not found."""
    store = _open_store(settings)
    try:
        candidates = [board] if board else hermes_api.boards()
        found = []
        for b in candidates:
            try:
                with hermes_api.connect(b) as conn:
                    if hermes_api.task(conn, card) is None:
                        continue
                    try:
                        found.append(
                            conditions.explain(conn, b, card, store, time.time(), settings=settings)
                        )
                    except Exception as exc:  # a real failure while explaining: never "not found"
                        print(t("explain.board_error", board=b, error=exc))
                        return 1
            except Exception as exc:  # board unreadable (connect / lookup)
                if board:
                    print(t("explain.board_error", board=b, error=exc))
                    return 1
        if not found:
            where = t("explain.where_board", board=board) if board else t("explain.where_any")
            print(t("explain.not_found", card=card, where=where))
            return 1
        if len(found) > 1:
            print(t("explain.ambiguous", card=card, boards=", ".join(c.board for c in found)))
            return 1
        print(conditions.format_explain(found[0]))
        return 0
    finally:
        store.close()


def run_status(settings: Settings) -> int:
    """Print the fleet summary across all boards."""
    store = _open_store(settings)
    try:
        print(conditions.format_status(conditions.fleet_status(store, time.time(), settings=settings)))
        return 0
    finally:
        store.close()


def run_doctor(settings: Settings, config_error: Optional[str] = None) -> None:
    """Print ``key: value`` diagnostics, one per line."""
    pid = LeaderLock(settings.shared_state_dir / "leader.lock").holder_pid(time.time())
    profiles = hermes_api.profiles_with_plugin()
    rows = [
        ("hermes_compat", hermes_api.HERMES_COMPAT),
        ("hermes_head", hermes_api.hermes_head() or t("doctor.unknown")),
        ("veto_hooks", t("doctor.present") if hermes_api.veto_hooks_available() else t("doctor.missing")),
        ("coordinator", str(pid) if pid is not None else t("doctor.none")),
        ("shared_state_dir", str(settings.shared_state_dir)),
        ("profiles_with_plugin", ", ".join(profiles) if profiles else t("doctor.none")),
    ]
    if config_error:
        rows.append(("config", t("doctor.config_invalid", error=config_error)))
    for key, value in rows:
        print(f"{key}: {value}")
