"""kanban-empowered: Hermes Agent plugin entry point."""
from __future__ import annotations

import atexit
import logging

log = logging.getLogger(__name__)


def register(ctx) -> None:
    """Wire the dispatch-tick hook and the ``kanban-empowered`` CLI command.

    The CLI command is registered even when the runtime part fails to start, so
    ``hermes kanban-empowered doctor`` stays usable to diagnose the failure.
    """
    from .ke import cli
    from .ke.settings import Settings

    settings = Settings.from_ctx(ctx)
    cli.bind(settings)
    try:
        from .ke.coordinator import Coordinator
        from .ke.store import Store

        store = Store(settings.shared_state_dir / "state.db")
        coordinator = Coordinator(settings, store)
        atexit.register(coordinator.leader.release)

        def on_tick(board=None, dry_run=False, result=None, **kw):
            coordinator.on_tick(board, result, dry_run=dry_run, **kw)

        ctx.register_hook("on_kanban_dispatch_tick", on_tick)
    except Exception:
        log.exception("kanban-empowered: failed to start the coordinator; hooks not registered")

    ctx.register_cli_command(
        "kanban-empowered",
        help="kanban-empowered diagnostics",
        setup_fn=cli.setup,
        handler_fn=cli.run,
        description="Human-wait reminders, stuck-card guards and infra-failure recovery.",
    )
