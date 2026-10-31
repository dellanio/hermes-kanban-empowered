"""``hermes kanban-empowered ...`` command line."""
from __future__ import annotations

import time
from typing import Any, Optional

from . import hermes_api
from .leader import LeaderLock
from .settings import Settings

_settings: Optional[Settings] = None


def bind(settings: Settings) -> None:
    """Give the CLI the settings ``register(ctx)`` built (Hermes calls handlers with ``args`` only)."""
    global _settings
    _settings = settings


def setup(parser: Any) -> None:
    """Build the ``kanban-empowered`` subcommand tree on the argparse parser Hermes hands over."""
    sub = parser.add_subparsers(dest="ke_cmd")
    sub.add_parser("doctor", help="Show plugin/Hermes compatibility and leader status")
    parser.set_defaults(ke_parser=parser)


def run(args: Any) -> int:
    """Dispatch a parsed ``kanban-empowered`` command."""
    settings = _settings if _settings is not None else Settings.from_mapping({})
    cmd = getattr(args, "ke_cmd", None)
    if cmd == "doctor":
        run_doctor(settings)
        return 0
    parser = getattr(args, "ke_parser", None)
    if parser is not None:
        parser.print_help()
    else:
        print("usage: hermes kanban-empowered doctor")
    return 0 if cmd is None else 2


def run_doctor(settings: Settings) -> None:
    """Print ``key: value`` diagnostics, one per line."""
    pid = LeaderLock(settings.shared_state_dir / "leader.lock").holder_pid(time.time())
    profiles = hermes_api.profiles_with_plugin()
    rows = [
        ("hermes_compat", hermes_api.HERMES_COMPAT),
        ("hermes_head", hermes_api.hermes_head() or "desconhecido"),
        ("veto_hooks", "presentes" if hermes_api.veto_hooks_available() else "ausentes"),
        ("leader", str(pid) if pid is not None else "nenhum"),
        ("shared_state_dir", str(settings.shared_state_dir)),
        ("profiles_com_plugin", ", ".join(profiles) if profiles else "nenhum"),
    ]
    for key, value in rows:
        print(f"{key}: {value}")
