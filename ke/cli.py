"""``hermes kanban-empowered ...`` command line."""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from . import board_status, conditions, guards_watch, hermes_api, inbound, panel, telegram
from .leader import LeaderLock
from .messages import render_done, t
from .settings import Settings
from .store import Store

WATCH_INTERVAL_S = 5

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
    st = sub.add_parser("status", help="Show what is running, waiting and resolved across boards")
    st.add_argument(
        "--watch", action="store_true", default=False,
        help="Live panel: redraw every %d s until Ctrl+C" % WATCH_INTERVAL_S,
    )
    dn = sub.add_parser("done", help="Resolve a card's human wait (same as replying \"done\" on Telegram)")
    dn.add_argument("card", help="card id (e.g. t_1a2b3c4d)")
    dn.add_argument("--board", default=None, help="board slug (default: search all boards)")
    rl = sub.add_parser("release", help="Hand a guarded card to another profile (active_pr / blocker_auth guards)")
    rl.add_argument("card", help="card id (e.g. t_1a2b3c4d)")
    rl.add_argument("--to", required=True, help="profile that takes the card over")
    rl.add_argument("--board", default=None, help="board slug (default: search all boards)")
    bs = sub.add_parser(
        "board-status",
        help="List the boards with their status, or set one (active/suspended/done; ativo/suspenso/concluído)",
    )
    bs.add_argument("board", nargs="?", default=None, help="board slug (omit to list every board)")
    bs.add_argument(
        "status", nargs="?", default=None,
        help="new status: active|suspended|done (or ativo|suspenso|concluído); omit to show it",
    )
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
        if getattr(args, "watch", False):
            return run_status_watch(settings)
        return run_status(settings)
    if cmd == "done":
        return run_done(settings, args.card, getattr(args, "board", None))
    if cmd == "release":
        return run_release(settings, args.card, args.to, getattr(args, "board", None))
    if cmd == "board-status":
        return run_board_status(settings, getattr(args, "board", None), getattr(args, "status", None))
    parser = getattr(args, "ke_parser", None)
    if parser is not None:
        parser.print_help()
    else:
        print("usage: hermes kanban-empowered {doctor,explain,status,done,release,board-status}")
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


def _os_user() -> str:
    try:
        import getpass

        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _locate_card(card: str, board: Optional[str]) -> Optional[str]:
    """The slug of the one board holding ``card`` (all boards, or only ``board``).

    Prints why and returns ``None`` when the card is not found, is on several boards, or the
    requested board cannot be read.
    """
    candidates = [board] if board else hermes_api.boards()
    found = []
    for b in candidates:
        try:
            with hermes_api.connect(b) as conn:
                if hermes_api.task(conn, card) is not None:
                    found.append(b)
        except Exception as exc:
            if board:
                print(t("explain.board_error", board=b, error=exc))
                return None
    if not found:
        where = t("explain.where_board", board=board) if board else t("explain.where_any")
        print(t("explain.not_found", card=card, where=where))
        return None
    if len(found) > 1:
        print(t("explain.ambiguous", card=card, boards=", ".join(found)))
        return None
    return found[0]


def run_release(settings: Settings, card: str, to_profile: str, board: Optional[str]) -> int:
    """Hand a guarded card to ``to_profile``. Exit 1 when nothing was released (the output says why)."""
    b = _locate_card(card, board)
    if b is None:
        return 1
    store = _open_store(settings)
    try:
        with hermes_api.connect(b) as conn:
            ok, text = guards_watch.release_result(
                conn, b, card, to_profile, hermes_api, store=store, now=time.time(), actor="cli:" + _os_user()
            )
        print(text)
        return 0 if ok else 1
    finally:
        store.close()


def run_done(settings: Settings, card: str, board: Optional[str]) -> int:
    """Resolve the open human wait of ``card`` from the command line. Exit 1 when nothing changed.

    Same state and version checks as a Telegram reply: the card must still be blocked on the
    block the plugin is tracking (its wait row), otherwise nothing is touched.
    """
    b = _locate_card(card, board)
    if b is None:
        return 1
    store = _open_store(settings)
    try:
        wait = store.latest_wait(b, card)
        if wait is None:
            print(t("done.no_wait", card=card, board=b))
            return 1
        who = "cli:" + _os_user()
        done, held = inbound.resolve_wait(
            hermes_api, store, b, card, int(wait["version"]),
            t("inbound.comment.cli", user=who), time.time(), who,
        )
        if not done:
            print(t("inbound.changed"))
            return 1
        print(render_done(b, card, held))
        return 0
    finally:
        store.close()


def _board_rows(boards: list, statuses: dict) -> list[str]:
    """One aligned ``slug  status  name`` line per board."""
    rows = [
        (b, board_status.label((statuses.get(b) or {}).get("status", "active")), hermes_api.board_name(b))
        for b in boards
    ]
    wb = max((len(r[0]) for r in rows), default=0)
    ws = max((len(r[1]) for r in rows), default=0)
    return [
        t("board_status.list.row", board=b.ljust(wb), status=st.ljust(ws), name=name if name != b else "").rstrip()
        for b, st, name in rows
    ]


def run_board_status(settings: Settings, board: Optional[str], status: Optional[str]) -> int:
    """``board-status``: list the boards with their status, show one, or set one (source ``cli``).

    Exit 0 on success (also when the board already had that status), 1 for an unknown board, 2 for an
    unknown status word.
    """
    if status is not None:
        wanted = board_status.normalize(status)
        if wanted is None:
            print(t("board_status.bad_status", status=status))
            return 2
    else:
        wanted = None
    boards = hermes_api.boards()
    if board is not None and board not in boards:
        print(t("board_status.unknown_board", board=board))
        return 1
    store = _open_store(settings)
    try:
        if wanted is None:
            lines = _board_rows([board] if board is not None else boards, store.board_statuses())
            print("\n".join(lines) if lines else t("board_status.list.empty"))
            return 0
        previous = store.set_board_status(board, wanted, int(time.time()), "cli")
        if previous is None:
            print(t("board_status.unchanged", board=board, status=board_status.label(wanted).lower()))
            return 0
        print(t("board_status.set", board=board, old=board_status.label(previous), new=board_status.label(wanted)))
        print(t("board_status.set.reactivated" if wanted == "active" else "board_status.set.held"))
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


_VT_ENABLED = False


def _enable_windows_vt() -> None:
    """Turn on ANSI escape processing for the Windows console once; silent no-op when unavailable."""
    global _VT_ENABLED
    if _VT_ENABLED or os.name != "nt":
        return
    _VT_ENABLED = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _clear_screen() -> None:
    """The ANSI clear-and-home sequence (VT processing is enabled on Windows first)."""
    _enable_windows_vt()
    print("\033[2J\033[H", end="", flush=True)


def run_status_watch(
    settings: Settings,
    interval: float = WATCH_INTERVAL_S,
    sleep: Any = time.sleep,
    clear: Any = _clear_screen,
) -> int:
    """Redraw the live panel every ``interval`` seconds until Ctrl+C (exit code 0).

    A failing refresh prints the error and the next refresh tries again.
    """
    store = _open_store(settings)
    try:
        while True:
            now = time.time()
            try:
                body = panel.render_terminal(panel.build_snapshot(store, hermes_api, now, settings=settings))
            except Exception as exc:
                body = t("status.watch.error", error=exc)
            clear()
            print(body)
            print()
            print(t("status.watch.hint", n=int(interval)))
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def _active_profile() -> str:
    try:
        return hermes_api.active_profile()
    except Exception:
        return t("doctor.unknown")


def _suspended_boards(settings: Settings) -> str:
    """The live boards whose status is not active (``slug`` / ``slug (Done)``), read-only; ``none`` when none."""
    try:
        statuses = board_status.StatusReader(settings.shared_state_dir / "state.db", ttl_s=0).statuses()
        names = []
        for b in hermes_api.boards():
            st = statuses.get(b, "active")
            if st == "suspended":
                names.append(b)
            elif st == "done":
                names.append(t("doctor.board_status", board=b, status=board_status.label(st)))
    except Exception:
        return t("doctor.unknown")
    return ", ".join(names) if names else t("doctor.none")


def run_doctor(settings: Settings, config_error: Optional[str] = None) -> None:
    """Print ``key: value`` diagnostics, one per line. Opens no Store (safe in a worker profile)."""
    pid = LeaderLock(settings.shared_state_dir / "leader.lock").holder_pid(time.time())
    profiles = hermes_api.profiles_with_plugin()
    token = hermes_api.telegram_token()  # via Hermes' secret scope; never printed
    rows = [
        ("hermes_compat", hermes_api.HERMES_COMPAT),
        ("hermes_head", hermes_api.hermes_head() or t("doctor.unknown")),
        ("veto_hooks", t("doctor.present") if hermes_api.veto_hooks_available() else t("doctor.missing")),
        ("coordinator", str(pid) if pid is not None else t("doctor.none")),
        ("active_profile", _active_profile()),
        ("coordinator_profile", settings.coordinator_profile),
        ("shared_state_dir", str(settings.shared_state_dir)),
        ("profiles_with_plugin", ", ".join(profiles) if profiles else t("doctor.none")),
        ("telegram_token", t("doctor.token.present") if token else t("doctor.token.missing")),
        ("owner_chats", t("doctor.owner_chats", n=len(settings.owner_ids))),
    ]
    rows.append(("boards_suspended", _suspended_boards(settings)))
    # what a suspended/done board holds: claim_task is hooked, claim_review_task (review lane) is not yet
    rows.append((
        "boards_hold_scope",
        t("doctor.hold_scope.partial") if hermes_api.veto_hooks_available() else t("doctor.hold_scope.none"),
    ))
    path = telegram.read_path_file(settings.shared_state_dir / "telegram_path")
    if path is not None:  # only once a client has recorded one; opens no Store
        kind = telegram.classify_path(path)
        rows.append(("telegram_path", t(f"doctor.telegram_path.{kind}", path=path) if kind != "unknown" else t("doctor.unknown")))
    if config_error:
        rows.append(("config", t("doctor.config_invalid", error=config_error)))
    for kind, _flat, nested, form_value, config_value in settings.conflicts:
        if kind == "override":
            text = t("doctor.conflict.override", form_value=form_value, config_key=nested, config_value=config_value)
        elif kind == "owner_id_differs":
            text = t("doctor.conflict.owner_id_differs", form_value=form_value, config_key=nested, config_value=config_value)
        else:
            text = t("doctor.conflict.owner_id", form_value=form_value, owner=config_value)
        rows.append(("config_conflict", text))
    for key, value in rows:
        print(f"{key}: {value}")
