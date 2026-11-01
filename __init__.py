"""kanban-empowered: Hermes Agent plugin entry point."""
from __future__ import annotations

import atexit
import logging

log = logging.getLogger(__name__)


def _apply_language(settings) -> None:
    """``settings.language`` overrides the Hermes language for the plugin's messages, and the
    configured role names (never shown on Telegram) are handed to the message layer."""
    try:
        from .ke import messages

        messages.set_language(settings.language)
        roles = set(settings.owner_ids)
        if settings.escalate_to:
            roles.add(settings.escalate_to)
        messages.set_roles(roles)
    except Exception:
        log.exception("kanban-empowered: could not apply the language setting")


def _register_worker_guard(ctx, settings) -> None:
    """Register the ``pre_tool_call`` guard in every profile (workers included).

    It is a safety check, so it also runs on the fallback defaults when the
    config is invalid (``require_structured_ask`` defaults to on).
    """
    try:
        from .ke import worker_guard

        def guard(tool_name="", args=None, **kw):
            return worker_guard.pre_tool_call(tool_name, args, settings=settings)

        ctx.register_hook("pre_tool_call", guard)
    except Exception:
        log.exception("kanban-empowered: failed to register the worker guard")


def _register_board_status_hook(ctx, settings) -> None:
    """Register the ``kanban_pre_claim`` veto (claims held on a suspended/done board), in every profile.

    Claims happen in whichever process dispatches, so every profile subscribes; each one reads the
    shared Store read-only (``settings.shared_state_dir``). Skipped when this Hermes build has no veto
    hooks (``doctor`` reports it); the callback itself is fail-open.
    """
    try:
        from .ke import hermes_api

        if not hermes_api.veto_hooks_available():
            return
        from .ke.board_status import StatusReader, make_pre_claim_hook

        reader = StatusReader(settings.shared_state_dir / "state.db")
        ctx.register_hook("kanban_pre_claim", make_pre_claim_hook(reader))
    except Exception:
        log.exception("kanban-empowered: failed to register the board status claim hook")


def _telegram_client(settings=None):
    """A Bot API client with the bot token Hermes resolves for this profile (``hermes_api.telegram_token``,
    through its secret scope), or ``None`` without a token.

    With ``settings`` the client records its last-good network path in ``<shared_state_dir>/telegram_path``
    (read by ``doctor``)."""
    from .ke import hermes_api

    token = hermes_api.telegram_token()
    if not token:
        return None
    from .ke.telegram import TelegramClient

    path_file = settings.shared_state_dir / "telegram_path" if settings is not None else None
    return TelegramClient(token, path_file=path_file)


def _register_inbound(ctx, settings, store, telegram) -> None:
    """Register the ``pre_gateway_dispatch`` reply handler, in the coordinator profile only."""
    try:
        from .ke import hermes_api
        from .ke.inbound import make_dispatch_hook

        if hermes_api.active_profile() != settings.coordinator_profile:
            return
        ctx.register_hook("pre_gateway_dispatch", make_dispatch_hook(settings, store, hermes_api, telegram))
    except Exception:
        log.exception("kanban-empowered: failed to register the Telegram reply handler")


def _register_infra(ctx, settings, infra) -> None:
    """Register the ``on_kanban_worker_exited`` recorder, in the coordinator profile only."""
    try:
        from .ke import hermes_api

        if hermes_api.active_profile() != settings.coordinator_profile:
            return

        def on_exited(**kw):
            try:
                infra.on_worker_exited(**kw)
            except Exception:  # an observer must never break the dispatcher
                log.exception("kanban-empowered: worker-exited hook failed")

        ctx.register_hook("on_kanban_worker_exited", on_exited)
    except Exception:
        log.exception("kanban-empowered: failed to register the worker-exited hook")


def _is_coordinator_profile(settings) -> bool:
    """True when this process runs as ``settings.coordinator_profile`` (False when that cannot be read)."""
    try:
        from .ke import hermes_api

        return hermes_api.active_profile() == settings.coordinator_profile
    except Exception:
        log.exception("kanban-empowered: could not read the active profile; coordinator not started")
        return False


def _register_cli(ctx, settings, config_error) -> None:
    """The ``kanban-empowered`` command. Isolated so a broken import (a Hermes module that moved)
    can neither take down the worker guard nor the coordinator."""
    try:
        from .ke import cli

        cli.bind(settings, config_error)
        ctx.register_cli_command(
            "kanban-empowered",
            help="kanban-empowered diagnostics",
            setup_fn=cli.setup,
            handler_fn=cli.run,
            description="Human-wait reminders, stuck-card guards and infra-failure recovery.",
        )
    except Exception:
        log.exception("kanban-empowered: failed to register the CLI command")


def _start_coordinator(ctx, settings) -> None:
    """Open the shared Store and wire the coordinator hooks, in the coordinator profile only.

    Worker profiles never open the read-write Store (no stray ``state.db``): they only need the guard.
    """
    if not _is_coordinator_profile(settings):
        return
    try:
        from .ke.coordinator import Coordinator
        from .ke.store import Store

        store = Store(settings.shared_state_dir / "state.db")
        coordinator = Coordinator(settings, store, telegram=_telegram_client(settings))
        atexit.register(coordinator.leader.release)

        def on_tick(board=None, dry_run=False, result=None, **kw):
            coordinator.on_tick(board, result, dry_run=dry_run, **kw)

        ctx.register_hook("on_kanban_dispatch_tick", on_tick)
        _register_inbound(ctx, settings, store, coordinator.telegram)
        _register_infra(ctx, settings, coordinator.infra)
    except Exception:
        log.exception("kanban-empowered: failed to start the coordinator; hooks not registered")


def register(ctx) -> None:
    """Wire the worker guard, the ``kanban-empowered`` CLI command and (coordinator profile) the hooks.

    The worker guard goes first and depends on nothing but the settings and ``ke.asks``; the CLI is
    registered even when the runtime part fails, so ``hermes kanban-empowered doctor`` stays usable
    to diagnose the failure. Each part fails on its own.
    """
    from .ke.settings import Settings

    config_error = None
    try:
        settings = Settings.from_ctx(ctx)
    except Exception as exc:
        log.exception("kanban-empowered: invalid config; falling back to defaults")
        settings = Settings.from_mapping({})
        config_error = str(exc) or type(exc).__name__
    _apply_language(settings)
    _register_worker_guard(ctx, settings)
    _register_board_status_hook(ctx, settings)
    _register_cli(ctx, settings, config_error)
    if config_error is None:  # never run the coordinator on guessed settings
        _start_coordinator(ctx, settings)
