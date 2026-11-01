"""Board (project) status: Ativo / Suspenso / Concluído (board-status-spec.md).

The status lives in the plugin Store (``board_status``), never in Hermes' ``board.json``. A non-active
board gets no reminders, its new claims are held by the ``kanban_pre_claim`` veto hook, the live panel
and the dashboard metrics leave it out, and reactivating it restarts the reminder ladder from that moment.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from conftest import settings_for_test


# -- Store: migration + CRUD ----------------------------------------------------------------------
def _store(tmp_path):
    from ke.store import Store

    return Store(tmp_path / "state" / "state.db")


def test_fresh_store_has_the_board_status_table_and_schema_version(tmp_path):
    from ke.store import SCHEMA_VERSION

    s = _store(tmp_path)
    cols = [r["name"] for r in s.conn.execute("PRAGMA table_info(board_status)").fetchall()]
    assert cols == ["board", "status", "changed_at", "changed_by"]
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 2
    s.close()


def test_migration_upgrades_a_pre_status_database(tmp_path):
    """A state.db written by the previous plugin version (no board_status, user_version 0) is migrated in place."""
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO kv VALUES ('panel_message_id', '42');"
    )
    old.commit()
    old.close()
    from ke.store import SCHEMA_VERSION, Store

    s = Store(path)
    assert s.kv_get("panel_message_id") == "42"  # existing data kept
    assert s.board_status("voz-hermes") == "active"
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()
    again = Store(path)  # reopening is a no-op
    assert again.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    again.close()


def test_status_check_constraint_rejects_unknown_values(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("INSERT INTO board_status VALUES ('x', 'paused', 1, 'test')")
    with pytest.raises(ValueError):
        s.set_board_status("x", "paused", 1, "test")
    s.close()


def test_unknown_board_defaults_to_active(tmp_path):
    s = _store(tmp_path)
    assert s.board_status("never-seen") == "active"
    assert s.board_statuses() == {}
    s.close()


def test_set_board_status_records_row_and_audit_action(tmp_path):
    s = _store(tmp_path)
    assert s.set_board_status("voz-hermes", "suspended", 1000, "desktop") == "active"
    assert s.board_status("voz-hermes") == "suspended"
    assert s.board_statuses() == {
        "voz-hermes": {"status": "suspended", "changed_at": 1000, "changed_by": "desktop"}
    }
    acts = s.recent_actions(["board_status"])
    assert [(a["board"], a["task_id"], a["detail"], a["ts"]) for a in acts] == [
        ("voz-hermes", "", "desktop: active->suspended", 1000)
    ]
    assert s.set_board_status("voz-hermes", "done", 1100, "cli") == "suspended"
    assert s.board_status("voz-hermes") == "done"
    assert s.recent_actions(["board_status"])[0]["detail"] == "cli: suspended->done"
    s.close()


def test_setting_the_same_status_is_a_no_op(tmp_path):
    s = _store(tmp_path)
    assert s.set_board_status("b", "active", 1000, "cli") is None  # unknown == active: nothing written
    assert s.board_statuses() == {}
    s.set_board_status("b", "suspended", 1000, "cli")
    assert s.set_board_status("b", "suspended", 2000, "desktop") is None
    assert s.board_statuses()["b"]["changed_at"] == 1000
    assert len(s.recent_actions(["board_status"])) == 1
    s.close()


def test_reactivation_resets_the_ladder_of_that_boards_waits(tmp_path):
    s = _store(tmp_path)
    s.upsert_wait("b", "t_1", 5, "sponsor", 100)
    s.mark_wait_sent("b", "t_1", 5, "escalation", 900)
    s.upsert_wait("other", "t_2", 6, "sponsor", 100)
    s.mark_wait_sent("other", "t_2", 6, "reminder1", 900)
    s.kv_set("esc_owner:b:t_1:5", "1")
    s.kv_set("esc_copy:b:t_1:5", "1")
    s.kv_set("esc_owner:other:t_2:6", "1")
    s.set_board_status("b", "suspended", 1000, "cli")
    assert s.latest_wait("b", "t_1")["stage"] == "escalation"  # suspending keeps the state
    s.set_board_status("b", "active", 2000, "cli")
    row = s.latest_wait("b", "t_1")
    assert (row["stage"], row["last_sent_at"], row["started_at"]) == (None, None, 100)
    assert s.latest_wait("other", "t_2")["stage"] == "reminder1"  # other boards untouched
    assert s.kv_get("esc_owner:b:t_1:5") is None and s.kv_get("esc_copy:b:t_1:5") is None
    assert s.kv_get("esc_owner:other:t_2:6") == "1"
    assert s.board_statuses()["b"] == {"status": "active", "changed_at": 2000, "changed_by": "cli"}
    assert s.recent_actions(["board_status"])[0]["detail"] == "cli: suspended->active"
    s.close()


# -- status names ---------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("active", "active"), ("ativo", "active"), ("Ativo", "active"),
    ("suspended", "suspended"), ("suspenso", "suspended"), ("SUSPENSO", "suspended"),
    ("done", "done"), ("concluido", "done"), ("concluído", "done"), ("Concluído", "done"),
    ("paused", None), ("", None), (None, None),
])
def test_normalize_status(raw, expected):
    from ke.board_status import normalize

    assert normalize(raw) == expected


def test_labels_follow_the_language(pt_br):  # noqa: F811
    from ke.board_status import label

    assert [label(s) for s in ("active", "suspended", "done")] == ["Ativo", "Suspenso", "Concluído"]


def test_labels_in_english():
    from ke.board_status import label

    assert [label(s) for s in ("active", "suspended", "done")] == ["Active", "Suspended", "Done"]


from test_waits import pt_br  # noqa: E402,F401 (pt_br is a fixture)


# -- read-only reader + the kanban_pre_claim veto hook ---------------------------------------------
class Tick:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _reader(tmp_path, clock=None):
    from ke.board_status import StatusReader

    return StatusReader(tmp_path / "state" / "state.db", clock=clock or Tick())


def test_reader_without_a_store_file_reports_active(tmp_path):
    r = _reader(tmp_path)
    assert r.status("anything") == "active"
    assert not (tmp_path / "state" / "state.db").exists()  # read-only: never creates the file


def test_reader_never_writes(tmp_path):
    s = _store(tmp_path)
    s.set_board_status("b", "suspended", 1, "cli")
    s.close()
    r = _reader(tmp_path)
    assert r.status("b") == "suspended"
    with pytest.raises(sqlite3.OperationalError):
        r._connect().execute("INSERT INTO kv VALUES ('x', 'y')")


class Where:
    """A stand-in board locator: ``{task_id: board}``; records every lookup.

    ``live`` is the list of live boards the hook may look at (default: every board named in ``cards``
    plus ``extra``); ``lists`` counts how often the hook asked for it.
    """

    def __init__(self, cards=None, live=None, step=None, clock=None):
        self.cards = dict(cards or {})
        self.calls = []
        self.lists = 0
        self._live = list(live) if live is not None else sorted(set(self.cards.values()))
        self.step = step
        self.clock = clock

    def __call__(self, board, task_id, *_rest):
        self.calls.append((board, task_id))
        if self.step:
            self.clock.now += self.step
        return self.cards.get(task_id) == board

    def live(self):
        self.lists += 1
        return list(self._live)


KW = {"assignee": "dev", "workspace_path": None, "claimer": "host:1", "telemetry_schema_version": 1}
SUSPENDED_EN = {"refuse": "Project suspended in Kanban Empowered — reactivate it to continue."}
DONE_EN = {"refuse": "Project marked done in Kanban Empowered — reactivate it to continue."}


def _statuses(tmp_path, **by_board):
    s = _store(tmp_path)
    for board, statuses in by_board.items():
        for i, st in enumerate(statuses.split(",")):
            s.set_board_status(board, st, i + 1, "cli")
    s.close()


def _hook(reader, where, **kw):
    from ke.board_status import make_pre_claim_hook

    return make_pre_claim_hook(reader, locate=where, boards=where.live, **kw)


def test_hook_refuses_cards_of_suspended_and_done_boards(tmp_path):
    """The card's own board decides, whatever ``board`` Hermes passes (it is the *current* board)."""
    _statuses(tmp_path, frozen="suspended", finished="done", back="suspended,active")
    where = Where({"t_f": "frozen", "t_d": "finished", "t_b": "back", "t_a": "default"})
    hook = _hook(_reader(tmp_path), where)
    assert hook(task_id="t_f", board="default", **KW) == SUSPENDED_EN
    assert hook(task_id="t_d", board="frozen", **KW) == DONE_EN
    assert hook(task_id="t_b", board="back", **KW) is None  # reactivated
    assert hook(task_id="t_a", board="frozen", **KW) is None  # the current board is suspended, the card's is not
    assert hook(task_id="t_unknown", board="default", **KW) is None
    assert hook(task_id=None, board="frozen", **KW) is None


def test_hook_looks_only_at_live_non_active_boards_and_the_hint_first(tmp_path):
    _statuses(tmp_path, a="suspended", b="done", c="suspended,active", gone="suspended")
    where = Where({"t_1": "b"}, live=["default", "a", "b", "c"])
    hook = _hook(_reader(tmp_path), where)
    assert hook(task_id="t_1", board="b") == DONE_EN
    assert where.calls == [("b", "t_1")]  # the board Hermes named is tried first
    where.calls.clear()
    assert hook(task_id="t_9", board="default") is None
    assert sorted(where.calls) == [("a", "t_9"), ("b", "t_9")]  # never the active ones, never a dead board


def test_hook_touches_no_board_while_every_board_is_active(tmp_path):
    def locate(*a):
        raise AssertionError("no board lookup expected")

    def boards():
        raise AssertionError("no board listing expected")

    from ke.board_status import make_pre_claim_hook

    assert make_pre_claim_hook(_reader(tmp_path), locate=locate, boards=boards)(task_id="t", board="default") is None
    _statuses(tmp_path, x="suspended,active")
    assert make_pre_claim_hook(_reader(tmp_path), locate=locate, boards=boards)(task_id="t", board="default") is None


def test_hook_reason_in_pt_br(tmp_path, pt_br):  # noqa: F811
    _statuses(tmp_path, frozen="suspended", finished="done")
    hook = _hook(_reader(tmp_path), Where({"t_f": "frozen", "t_d": "finished"}))
    assert hook(task_id="t_f", board="default") == {
        "refuse": "Projeto suspenso no Kanban Empowered — reative para continuar."
    }
    assert hook(task_id="t_d", board="default") == {
        "refuse": "Projeto concluído no Kanban Empowered — reative para continuar."
    }


def test_hook_fails_open_on_store_error(tmp_path):
    from ke.board_status import make_pre_claim_hook

    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a sqlite database" * 100)
    assert _hook(_reader(tmp_path), Where({"t": "b"}))(task_id="t", board="b") is None

    class Broken:
        def statuses(self):
            raise RuntimeError("boom")

    where = Where({"t": "b"})
    assert make_pre_claim_hook(Broken(), locate=where, boards=where.live)(task_id="t", board="b") is None


def test_hook_fails_open_when_the_board_listing_fails(tmp_path):
    from ke.board_status import make_pre_claim_hook

    _statuses(tmp_path, frozen="suspended")

    def boards():
        raise OSError("boards dir unreadable")

    hook = make_pre_claim_hook(_reader(tmp_path), locate=Where({"t": "frozen"}), boards=boards)
    assert hook(task_id="t", board="frozen") is None


def test_hook_fails_open_when_the_store_is_locked(tmp_path):
    """A writer holding an exclusive lock: the read gives up after its short busy timeout, fail-open."""
    from ke.board_status import StatusReader

    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.conn.execute("PRAGMA journal_mode=DELETE")  # WAL readers never block; a rollback journal does
    s.conn.execute("BEGIN EXCLUSIVE")
    try:
        reader = StatusReader(tmp_path / "state" / "state.db", busy_ms=50, clock=Tick())
        started = time.monotonic()
        assert _hook(reader, Where({"t": "frozen"}))(task_id="t", board="frozen") is None
        assert time.monotonic() - started < 2.0
    finally:
        s.conn.execute("ROLLBACK")
        s.close()


# -- hook cost: one listing per scan, lookup cache, time budget (review Important #2) ----------------
def test_hook_lists_the_live_boards_once_per_scan(tmp_path):
    _statuses(tmp_path, **{f"b{i}": "suspended" for i in range(6)})
    where = Where({}, live=[f"b{i}" for i in range(6)])
    clock = Tick()
    hook = _hook(_reader(tmp_path, clock), where, clock=clock)
    assert hook(task_id="t_new", board="default") is None
    assert where.lists == 1 and len(where.calls) == 6


def test_hook_caches_a_hit_until_the_statuses_change(tmp_path):
    from ke.board_status import CACHE_TTL_S

    _statuses(tmp_path, frozen="suspended", other="done")
    clock = Tick()
    where = Where({"t_f": "frozen"}, live=["frozen", "other"])
    hook = _hook(_reader(tmp_path, clock), where, clock=clock)
    for _ in range(3):
        assert hook(task_id="t_f", board="default") == SUSPENDED_EN
        clock.now += 60  # every dispatcher tick retries the held card
    assert where.calls == [("frozen", "t_f")] and where.lists == 1  # one scan, then the cache
    s = _store(tmp_path)
    s.set_board_status("third", "suspended", 9, "cli")  # the statuses change: the cache is dropped
    s.close()
    clock.now += CACHE_TTL_S + 1
    assert hook(task_id="t_f", board="default") == SUSPENDED_EN
    assert len(where.calls) == 2


def test_hook_caches_a_miss_for_a_short_ttl(tmp_path):
    from ke.board_status import NEGATIVE_TTL_S

    assert 0 < NEGATIVE_TTL_S <= 60
    _statuses(tmp_path, frozen="suspended")
    clock = Tick()
    where = Where({"t_live": "default"}, live=["default", "frozen"])
    hook = _hook(_reader(tmp_path, clock), where, clock=clock)
    assert hook(task_id="t_live", board="default") is None
    clock.now += NEGATIVE_TTL_S - 1
    assert hook(task_id="t_live", board="default") is None
    assert len(where.calls) == 1
    clock.now += 2
    assert hook(task_id="t_live", board="default") is None
    assert len(where.calls) == 2  # expired: looked up again


def test_hook_cache_is_bounded(tmp_path):
    from ke.board_status import LOOKUP_CACHE_MAX

    _statuses(tmp_path, frozen="suspended")
    clock = Tick()
    where = Where({}, live=["frozen"])
    hook = _hook(_reader(tmp_path, clock), where, clock=clock)
    for i in range(LOOKUP_CACHE_MAX + 10):
        hook(task_id=f"t_{i}", board="default")
    calls = len(where.calls)
    hook(task_id="t_0", board="default")  # the oldest entry was evicted
    assert len(where.calls) == calls + 1
    hook(task_id=f"t_{LOOKUP_CACHE_MAX + 9}", board="default")  # the newest is still cached
    assert len(where.calls) == calls + 1


def test_hook_scan_has_a_time_budget_well_under_the_core_cap(tmp_path, caplog):
    """Many non-active boards and slow lookups: the scan stops at the budget, fails open and says so once."""
    import logging

    from ke import board_status
    from ke.board_status import SCAN_BUDGET_S

    assert 0 < SCAN_BUDGET_S <= 1.0  # the core abandons the callback at 3 s
    boards = [f"done-{i:03d}" for i in range(300)]
    _statuses(tmp_path, **{b: "done" for b in boards})
    clock = Tick()
    where = Where({"t_x": boards[-1]}, live=boards, step=0.01, clock=clock)  # 10 ms per lookup
    board_status._budget_warned = False
    hook = _hook(_reader(tmp_path, clock), where, clock=clock)
    with caplog.at_level(logging.WARNING, logger="ke.board_status"):
        assert hook(task_id="t_x", board="default") is None  # budget ran out before the last board
        first = len(where.calls)
        assert first <= SCAN_BUDGET_S / 0.01 + 2
        assert hook(task_id="t_x", board="default") is None  # not cached: scanned (and cut) again
        assert len(where.calls) > first
    warnings = [r for r in caplog.records if "budget" in r.getMessage()]
    assert len(warnings) == 1  # once per process


def test_hook_with_many_real_non_active_boards_scans_once_per_card(kanban_home, tmp_path, monkeypatch):
    """Real boards: the second claim attempt of the same ready card opens no board at all."""
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api
    from ke.board_status import StatusReader, make_pre_claim_hook
    from conftest import create_task

    s = _store(tmp_path)
    for i in range(8):
        kbb.create_board(f"old-{i}")
        with hermes_api.connect(f"old-{i}") as conn:
            pass
        s.set_board_status(f"old-{i}", "done", 1, "cli")
    with hermes_api.connect("default") as conn:
        live = create_task(conn, title="live", assignee="dev")
    with hermes_api.connect("old-5") as conn:
        frozen = create_task(conn, title="frozen", assignee="dev")
    s.close()
    hook = make_pre_claim_hook(StatusReader(tmp_path / "state" / "state.db"))
    opened = []
    real_connect = hermes_api.connect

    def counting_connect(board):
        opened.append(board)
        return real_connect(board)

    monkeypatch.setattr(hermes_api, "connect", counting_connect)
    started = time.monotonic()
    assert hook(task_id=live, board="default", **KW) is None
    assert hook(task_id=frozen, board="default", **KW) == DONE_EN
    assert time.monotonic() - started < 1.0
    n = len(opened)
    assert hook(task_id=live, board="default", **KW) is None
    assert hook(task_id=frozen, board="default", **KW) == DONE_EN
    assert len(opened) == n  # both answers came from the cache


def test_reader_caches_for_at_most_the_ttl(tmp_path):
    from ke.board_status import CACHE_TTL_S

    assert 0 < CACHE_TTL_S <= 5
    clock = Tick(100.0)
    r = _reader(tmp_path, clock)
    s = _store(tmp_path)
    assert r.status("b") == "active"
    s.set_board_status("b", "suspended", 1, "cli")
    clock.now += CACHE_TTL_S - 0.5
    assert r.status("b") == "active"  # still cached
    clock.now += 1.0
    assert r.status("b") == "suspended"  # expired: read again
    s.close()


def test_reader_does_not_cache_errors(tmp_path):
    clock = Tick(100.0)
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"garbage" * 1000)
    r = _reader(tmp_path, clock)
    with pytest.raises(Exception):
        r.status("b")
    path.unlink()
    s = _store(tmp_path)
    s.set_board_status("b", "done", 1, "cli")
    assert r.status("b") == "done"  # no clock advance needed: the failure was not cached
    s.close()


# -- hermes_api wrappers ----------------------------------------------------------------------------
def test_board_exists_wrapper(kanban_home):
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api

    kbb.create_board("voz-hermes", name="Canal de Voz do Hermes")
    assert hermes_api.board_exists("default") and hermes_api.board_exists("voz-hermes")
    assert not hermes_api.board_exists("nope")
    assert not hermes_api.board_exists("")


def test_task_exists_wrapper(kanban_home):
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api
    from conftest import create_task

    kbb.create_board("voz-hermes")
    with hermes_api.connect("voz-hermes") as conn:
        tid = create_task(conn, title="x")
    assert hermes_api.task_exists("voz-hermes", tid)
    assert not hermes_api.task_exists("default", tid)
    assert not hermes_api.task_exists("voz-hermes", "t_00000000")
    root = kanban_home / "kanban" / "boards"
    assert not hermes_api.task_exists("never-created", tid)
    assert not (root / "never-created").exists()  # an unknown board is never created by a lookup


# -- register(ctx) wiring ---------------------------------------------------------------------------
class _Ctx:
    def __init__(self, settings=None):
        self.hooks = {}
        self._settings = settings or {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_cli_command(self, *a, **kw):
        pass

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def test_register_subscribes_pre_claim_in_any_profile_when_the_hooks_exist(
    load_plugin, kanban_home, tmp_path, monkeypatch
):
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api
    from conftest import create_task

    kbb.create_board("frozen")
    with hermes_api.connect("frozen") as conn:
        frozen = create_task(conn, title="frozen work", assignee="dev")
    with hermes_api.connect("default") as conn:
        live = create_task(conn, title="live work", assignee="dev")
    mod = load_plugin()
    api = __import__(mod.__name__ + ".ke.hermes_api", fromlist=["x"])
    monkeypatch.setattr(api, "active_profile", lambda: "worker-x")
    monkeypatch.setattr(api, "veto_hooks_available", lambda: True)
    ctx = _Ctx({"shared_state_dir": str(tmp_path / "state")})
    mod.register(ctx)
    assert "kanban_pre_claim" in ctx.hooks and "pre_tool_call" in ctx.hooks
    store_mod = __import__(mod.__name__ + ".ke.store", fromlist=["x"])
    s = store_mod.Store(tmp_path / "state" / "state.db")
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.close()
    (cb,) = ctx.hooks["kanban_pre_claim"]
    out = cb(task_id=frozen, board="default", assignee="dev", workspace_path=None, claimer="h", telemetry_schema_version=1)
    assert isinstance(out, dict) and "suspended" in out["refuse"]
    assert cb(task_id=live, board="default", assignee="dev", workspace_path=None, claimer="h") is None


def test_register_skips_pre_claim_without_the_veto_hooks(load_plugin, kanban_home, tmp_path, monkeypatch):
    mod = load_plugin()
    api = __import__(mod.__name__ + ".ke.hermes_api", fromlist=["x"])
    monkeypatch.setattr(api, "active_profile", lambda: "worker-x")
    monkeypatch.setattr(api, "veto_hooks_available", lambda: False)
    ctx = _Ctx({"shared_state_dir": str(tmp_path / "state")})
    mod.register(ctx)
    assert "kanban_pre_claim" not in ctx.hooks
    assert "pre_tool_call" in ctx.hooks


@pytest.fixture
def subscribed(kanban_home, tmp_path):
    """The plugin's kanban_pre_claim callback subscribed to the installed Hermes' plugin manager."""
    from hermes_cli import plugins as hp
    from ke import hermes_api
    from ke.board_status import StatusReader, make_pre_claim_hook

    if not hermes_api.veto_hooks_available():
        pytest.skip("this Hermes build has no veto hooks")
    hook = make_pre_claim_hook(StatusReader(tmp_path / "state" / "state.db"))
    manager = hp.get_plugin_manager()
    manager._hooks.setdefault("kanban_pre_claim", []).append(hook)
    yield
    manager._hooks["kanban_pre_claim"].remove(hook)


def test_real_claim_on_a_suspended_board_is_held_like_the_gateway_dispatcher_does_it(subscribed, tmp_path):
    """End to end against the installed Hermes. The gateway dispatcher claims board X with a connection to X
    but without pinning X as the current board, so the hook's ``board`` is the *current* board (``default``):
    the card's own board must still decide."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api
    from conftest import create_task

    kbb.create_board("frozen")
    with hermes_api.connect("frozen") as conn:
        frozen = create_task(conn, title="frozen work", assignee="dev")
    with hermes_api.connect("default") as conn:
        live = create_task(conn, title="live work", assignee="dev")
    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    with hermes_api.connect("frozen") as conn:
        assert kb.get_current_board() == "default"
        assert kb.claim_task(conn, frozen) is None
        assert hermes_api.task(conn, frozen).status == "ready"
        refused = [e for e in hermes_api.events(conn, frozen) if e.kind == "claim_refused"]
        assert refused and "Project suspended in Kanban Empowered" in str(refused[-1].payload)
    with hermes_api.connect("default") as conn:
        assert kb.claim_task(conn, live) is not None  # other boards claim normally
    s.close()


def test_real_claim_when_the_current_board_is_the_suspended_one(subscribed, tmp_path, monkeypatch):
    """The reverse mistake: the current board is suspended, the claimed card lives on an active board."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api
    from conftest import create_task

    kbb.create_board("frozen")
    with hermes_api.connect("default") as conn:
        live = create_task(conn, title="live work", assignee="dev")
    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.close()
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "frozen")
    assert kb.get_current_board() == "frozen"
    with hermes_api.connect("default") as conn:
        assert kb.claim_task(conn, live) is not None


# -- reminders: no ladder on a non-active board, restart on reactivation ----------------------------
H = 3600
D = 86400


def _tracker(tmp_path):
    from ke import hermes_api
    from ke.store import Store
    from ke.waits import WaitTracker

    settings = settings_for_test(tmp_path)
    store = Store(settings.shared_state_dir / "state.db")
    return WaitTracker(settings, store, hermes_api), store


def _blocked(board="default", **kw):
    from ke import hermes_api
    from test_waits import make_human_block

    with hermes_api.connect(board) as conn:
        tid, ev = make_human_block(conn, **kw)
    return tid, ev


@pytest.mark.parametrize("status", ["suspended", "done"])
def test_tracker_sends_nothing_for_a_non_active_board(kanban_home, clock, tmp_path, status):
    wt, store = _tracker(tmp_path)
    tid, ev = _blocked()
    clock.now = float(ev.created_at)
    wt.sync(clock.now)
    store.set_board_status("default", status, int(clock.now), "cli")
    for offset in (1800, 9 * H, 3 * D):
        clock.now = ev.created_at + offset
        wt.sync(clock.now)
        assert wt.collect_due(clock.now) == [], offset
    assert store.latest_wait("default", tid) is not None  # still tracked, only silent


def test_reactivation_restarts_the_ladder_from_the_reactivation_time(kanban_home, clock, tmp_path):
    wt, store = _tracker(tmp_path)
    tid, ev = _blocked()
    t0 = ev.created_at
    wt.sync(float(t0))
    (due,) = wt.collect_due(float(t0 + 9 * H))
    assert due.stage == "escalation"
    wt.mark_sent(due, float(t0 + 9 * H))
    store.set_board_status("default", "suspended", t0 + 10 * H, "desktop")
    assert wt.collect_due(float(t0 + 3 * D)) == []
    back = t0 + 4 * D
    store.set_board_status("default", "active", back, "desktop")

    def stages(at):
        wt.sync(float(at))
        return [d.stage for d in wt.collect_due(float(at))]

    assert stages(back) == []  # no instant flood of the old stages
    assert stages(back + 1799) == []
    (first,) = wt.collect_due(float(back + 1800))
    assert first.stage == "reminder1"
    assert first.age_s == back + 1800 - t0  # the message still tells the real age of the request
    wt.mark_sent(first, float(back + 1800))
    assert stages(back + 7199) == []
    assert stages(back + 7200) == ["reminder2"]
    wt.mark_sent(wt.collect_due(float(back + 7200))[0], float(back + 7200))
    assert stages(back + 8 * H) == ["escalation"]


def test_a_block_newer_than_the_reactivation_keeps_its_own_clock(kanban_home, clock, tmp_path):
    wt, store = _tracker(tmp_path)
    store.set_board_status("default", "suspended", 1, "cli")
    store.set_board_status("default", "active", 2, "cli")
    tid, ev = _blocked()
    wt.sync(float(ev.created_at))
    assert [d.stage for d in wt.collect_due(float(ev.created_at + 1800))] == ["reminder1"]


def test_other_boards_keep_their_reminders(kanban_home, clock, tmp_path):
    from hermes_cli import kanban_db_boards as kbb

    kbb.create_board("side")
    wt, store = _tracker(tmp_path)
    frozen, ev1 = _blocked("side")
    live, ev2 = _blocked("default")
    store.set_board_status("side", "suspended", 1, "cli")
    start = max(ev1.created_at, ev2.created_at)
    wt.sync(float(start))
    assert [(d.board, d.task_id) for d in wt.collect_due(float(start + 1800))] == [("default", live)]


OWNER_CHAT = 111


class _Api:
    """The real adapter, with the veto hooks reported present (no setup alert in the way)."""

    def __init__(self):
        from ke import hermes_api

        self._api = hermes_api

    def veto_hooks_available(self):
        return True

    def __getattr__(self, name):
        return getattr(self._api, name)


def _coordinator(tmp_path, clock, fake_tg, **kw):
    from conftest import make_coordinator

    kw.setdefault("owner_ids", {"sponsor": OWNER_CHAT})
    return make_coordinator(tmp_path, clock=clock, telegram=fake_tg, api=_Api(), **kw)


def test_coordinator_sends_no_reminder_for_a_suspended_board_then_resumes(kanban_home, clock, tmp_path, fake_tg):
    coord = _coordinator(tmp_path, clock, fake_tg)
    tid, ev = _blocked()
    clock.now = float(ev.created_at)
    coord.evaluate(clock.now)
    coord.store.set_board_status("default", "suspended", int(clock.now), "desktop")
    for hours in (1, 3, 9, 30):
        clock.now = float(ev.created_at + hours * H)
        coord.evaluate(clock.now)
    assert fake_tg.count("send") == 0  # no reminder, escalation or digest
    back = ev.created_at + 31 * H
    coord.store.set_board_status("default", "active", back, "desktop")
    clock.now = float(back + 60)
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 0
    clock.now = float(back + 1800)
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 1
    assert coord.store.latest_wait("default", tid)["stage"] == "reminder1"


def test_no_escalation_setup_alert_for_a_suspended_boards_old_wait(kanban_home, clock, tmp_path, fake_tg):
    coord = _coordinator(tmp_path, clock, fake_tg, escalate_to=None)
    tid, ev = _blocked()
    coord.store.set_board_status("default", "suspended", 1, "cli")
    clock.now = float(ev.created_at)
    coord.evaluate(clock.now)
    clock.now = float(ev.created_at + 9 * H)
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 0
    assert coord.store.kv_get("setup_escalation_no_target_sent") is None


def test_no_layout_resend_for_a_suspended_board(kanban_home, clock, tmp_path, fake_tg):
    coord = _coordinator(tmp_path, clock, fake_tg)
    tid, ev = _blocked()
    clock.now = float(ev.created_at)
    coord.waits.sync(clock.now)
    coord.store.mark_wait_sent("default", tid, int(ev.id), "reminder1", int(clock.now))
    coord.store.set_board_status("default", "suspended", int(clock.now), "cli")
    coord.store.kv_set("template_version", "0")  # an older layout: open waits would be re-sent once
    clock.now += 60
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 0


def test_explain_says_the_reminders_are_paused(kanban_home, clock, tmp_path):
    from ke import conditions, hermes_api

    wt, store = _tracker(tmp_path)
    tid, ev = _blocked()
    wt.sync(float(ev.created_at))
    store.set_board_status("default", "suspended", 1, "cli")
    with hermes_api.connect("default") as conn:
        cc = conditions.explain(conn, "default", tid, store, ev.created_at + 600, settings_for_test(tmp_path))
    wait = next(c for c in cc.conditions if c.name == "wait")
    assert "reminders paused (project suspended)" in wait.text and "next reminder" not in wait.text


def test_explain_next_reminder_is_anchored_to_the_reactivation(kanban_home, clock, tmp_path):
    from ke import conditions, hermes_api

    wt, store = _tracker(tmp_path)
    tid, ev = _blocked()
    wt.sync(float(ev.created_at))
    back = ev.created_at + 2 * D
    store.set_board_status("default", "suspended", ev.created_at + 1, "cli")
    store.set_board_status("default", "active", back, "cli")
    with hermes_api.connect("default") as conn:
        cc = conditions.explain(conn, "default", tid, store, back + 600, settings_for_test(tmp_path))
    wait = next(c for c in cc.conditions if c.name == "wait")
    assert "next reminder in 20 min" in wait.text, wait.text


# -- live panel ---------------------------------------------------------------------------------------
def test_panel_snapshot_excludes_non_active_boards_and_counts_them(kanban_home, tmp_path):
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api as h
    from ke.panel import build_snapshot
    from test_conditions import _claim
    from conftest import create_task

    kbb.create_board("voz-hermes")
    kbb.create_board("portal")
    store = _store(tmp_path)
    live, _ = _blocked("default", title="live wait")
    _blocked("voz-hermes", title="frozen wait")
    with h.connect("voz-hermes") as conn:
        run_t = create_task(conn, title="still running", assignee="dev")
        _claim(conn, run_t)
    now = time.time()
    store.upsert_guard_alert("voz-hermes", "t_guard", "active_pr", int(now) - 4000)
    store.upsert_guard_alert("default", "t_guard2", "active_pr", int(now) - 4000)
    store.set_board_status("voz-hermes", "suspended", 1, "cli")
    store.set_board_status("portal", "done", 1, "cli")
    store.set_board_status("archived-or-gone", "suspended", 1, "cli")  # not a live board: not counted
    snap = build_snapshot(store, h, now + 1)
    assert [w["task_id"] for w in snap.human_waits] == [live]
    assert snap.running == []
    assert [g["task_id"] for g in snap.held] == ["t_guard2"]
    assert snap.suspended_boards == 2
    store.close()


def test_panel_text_ends_with_the_suspended_projects_line():
    from ke.panel import render_telegram, render_terminal
    from test_panel import snapshot_fixture

    assert "suspended project" not in render_telegram(snapshot_fixture())
    text = render_telegram(snapshot_fixture(suspended_boards=2))
    assert "⏸ 2 suspended projects" in text
    quiet = render_telegram(snapshot_fixture(suspended_boards=1, human_waits=[]))
    assert quiet.rstrip().splitlines()[-1] == "⏸ 1 suspended project"  # the very last line when no reply hint
    assert "⏸ 2 suspended projects" in render_terminal(snapshot_fixture(suspended_boards=2))


def test_panel_suspended_line_in_pt_br(pt_br):  # noqa: F811
    from ke.panel import render_telegram
    from test_panel import snapshot_fixture

    assert "⏸ 2 projetos suspensos" in render_telegram(snapshot_fixture(suspended_boards=2))
    assert "⏸ 1 projeto suspenso" in render_telegram(snapshot_fixture(suspended_boards=1))


# -- dashboard backend --------------------------------------------------------------------------------
from test_dashboard_api import (  # noqa: E402,F401 (fixtures)
    ASK_OPTIONS, ASK_WORD, PREFIX, _status, _waiting, api_mod, client, store,
)


def _two_boards():
    from hermes_cli import kanban_db_boards as kbb

    kbb.create_board("voz-hermes", name="Canal de Voz do Hermes")
    kbb.create_board("portal", name="Portal Investidor Imobiliário")


def test_overview_moves_suspended_items_out_of_needs_you_and_metrics(client, store):
    from hermes_cli import kanban_db as kb
    from ke import hermes_api

    _two_boards()
    live, _ = _waiting(ASK_WORD, title="live")
    frozen, _ = _waiting(ASK_WORD, board="voz-hermes", held=2, title="frozen")
    finished, _ = _waiting(ASK_OPTIONS, board="portal", title="finished")
    with hermes_api.connect("voz-hermes") as conn:
        run_t = __import__("conftest").create_task(conn, title="still running", assignee="dev")
        assert kb.claim_task(conn, run_t) is not None
    store.set_board_status("voz-hermes", "suspended", int(time.time()), "cli")
    store.set_board_status("portal", "done", int(time.time()), "cli")
    body = client.get(PREFIX + "/overview").json()
    assert [i["task_id"] for i in body["needs_you"]] == [live]
    assert body["needs_you"][0]["board_status"] == "active"
    suspended = {i["task_id"]: i for i in body["suspended"]}
    assert set(suspended) == {frozen, finished}
    assert suspended[frozen]["board_status"] == "suspended" and suspended[frozen]["status"] == "suspended"
    assert suspended[finished]["board_status"] == "done"
    assert suspended[frozen]["kind"] == "human_wait" and suspended[frozen]["held_behind_count"] == 2
    assert suspended[frozen]["reminders_paused"] is True and suspended[frozen]["next_reminder_in_s"] == 0
    assert suspended[finished]["options"]  # same shape as a needs_you item
    m = body["metrics"]
    assert (m["needs_you"], m["impacted_cards"], m["workers_running"]) == (1, 0, 0)
    assert [r["task_id"] for r in body["running"]] == [run_t]  # running workers are not interrupted
    assert body["running"][0]["board_status"] == "suspended"
    assert body["boards_status"] == [
        {"board": "default", "board_title": "Default", "status": "active"},
        {"board": "portal", "board_title": "Portal Investidor Imobiliário", "status": "done"},
        {"board": "voz-hermes", "board_title": "Canal de Voz do Hermes", "status": "suspended"},
    ]


def test_overview_board_filter_on_a_suspended_board(client, store):
    _two_boards()
    frozen, _ = _waiting(ASK_WORD, board="voz-hermes")
    store.set_board_status("voz-hermes", "suspended", 1, "cli")
    only = client.get(PREFIX + "/overview", params={"board": "voz-hermes"}).json()
    assert only["needs_you"] == [] and [i["task_id"] for i in only["suspended"]] == [frozen]
    assert only["metrics"]["needs_you"] == 0


def test_count_badge_leaves_suspended_boards_out(client, store):
    _two_boards()
    _waiting(ASK_WORD)
    _waiting(ASK_WORD, board="voz-hermes")
    assert client.get(PREFIX + "/count").json()["needs_you"] == 2
    store.set_board_status("voz-hermes", "suspended", 1, "cli")
    assert client.get(PREFIX + "/count").json()["needs_you"] == 1


def test_item_of_a_suspended_board_still_opens(client, store):
    _two_boards()
    frozen, version = _waiting(ASK_WORD, board="voz-hermes")
    store.set_board_status("voz-hermes", "suspended", 1, "cli")
    res = client.get(PREFIX + "/item", params={"board": "voz-hermes", "task_id": frozen})
    assert res.status_code == 200
    item = res.json()
    assert item["board_status"] == "suspended" and item["version"] == version and item["reminder_text"]


def test_answer_on_a_suspended_item_still_resolves(client, store):
    _two_boards()
    frozen, version = _waiting(ASK_WORD, board="voz-hermes")
    store.set_board_status("voz-hermes", "suspended", 1, "cli")
    res = client.post(PREFIX + "/answer", json={
        "board": "voz-hermes", "task_id": frozen, "version": version, "text": "connected",
    }).json()
    assert res["result"] == "resolved"
    assert _status(frozen, board="voz-hermes") != "blocked"


def test_post_board_status_sets_audits_and_refreshes(client, store):
    _two_boards()
    res = client.post(PREFIX + "/board-status", json={"board": "voz-hermes", "status": "suspended"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "status": "suspended", "previous": "active", "changed": True}
    assert store.board_status("voz-hermes") == "suspended"
    (act,) = store.recent_actions(["board_status"])
    assert act["board"] == "voz-hermes" and act["detail"] == "desktop: active->suspended"
    again = client.post(PREFIX + "/board-status", json={"board": "voz-hermes", "status": "suspended"}).json()
    assert again == {"ok": True, "status": "suspended", "previous": "suspended", "changed": False}
    body = client.get(PREFIX + "/overview").json()
    assert {"board": "voz-hermes", "board_title": "Canal de Voz do Hermes", "status": "suspended"} in body["boards_status"]
    tl = [e for e in body["timeline"] if e["kind"] == "board_status"]
    assert tl and tl[0]["text"] == "Project Canal de Voz do Hermes suspended" and tl[0]["detail"] == "suspended"
    client.post(PREFIX + "/board-status", json={"board": "voz-hermes", "status": "active"})
    texts = [e["text"] for e in client.get(PREFIX + "/overview").json()["timeline"] if e["kind"] == "board_status"]
    assert texts[0] == "Project Canal de Voz do Hermes reactivated"
    client.post(PREFIX + "/board-status", json={"board": "portal", "status": "done"})
    texts = [e["text"] for e in client.get(PREFIX + "/overview").json()["timeline"] if e["kind"] == "board_status"]
    assert texts[0] == "Project Portal Investidor Imobiliário marked done"


def test_post_board_status_timeline_in_pt_br(client, store, pt_br):  # noqa: F811
    _two_boards()
    for status in ("suspended", "active", "done"):
        client.post(PREFIX + "/board-status", json={"board": "voz-hermes", "status": status})
    texts = [e["text"] for e in client.get(PREFIX + "/overview").json()["timeline"] if e["kind"] == "board_status"]
    assert texts == [
        "Projeto Canal de Voz do Hermes concluído",
        "Projeto Canal de Voz do Hermes reativado",
        "Projeto Canal de Voz do Hermes suspenso",
    ]


@pytest.mark.parametrize("body", [
    {"board": "default", "status": "paused"},
    {"board": "default", "status": "suspenso"},  # the API takes the internal names only
    {"board": "default"},
    {"status": "active"},
    {"board": "../etc", "status": "active"},
])
def test_post_board_status_validation_422(client, body):
    assert client.post(PREFIX + "/board-status", json=body).status_code == 422


def test_post_board_status_unknown_board_404(client, store):
    res = client.post(PREFIX + "/board-status", json={"board": "nope", "status": "suspended"})
    assert res.status_code == 404
    assert store.board_statuses() == {}


def test_i18n_has_the_status_strings(client):
    strings = client.get(PREFIX + "/i18n").json()["strings"]
    for key in ("ui.status.active", "ui.status.suspended", "ui.status.done", "ui.section.suspended",
                "ui.projects", "ui.projects.note", "ui.needs.reminders_paused", "ui.projects.failed"):
        assert strings[key], key


# -- CLI: hermes kanban-empowered board-status [<slug> [status]] + doctor ---------------------------
def _cli_settings(tmp_path):
    return settings_for_test(tmp_path)


def _cli_store(settings):
    from ke.store import Store

    return Store(settings.shared_state_dir / "state.db")


def test_cli_lists_every_board_with_its_status(kanban_home, tmp_path, capsys):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    st = _cli_store(s)
    st.set_board_status("voz-hermes", "suspended", 1, "desktop")
    st.set_board_status("portal", "done", 1, "desktop")
    st.close()
    assert cli.run_board_status(s, None, None) == 0
    lines = [ln.split() for ln in capsys.readouterr().out.strip().splitlines()]
    assert [ln[:2] for ln in lines] == [["default", "Active"], ["portal", "Done"], ["voz-hermes", "Suspended"]]
    assert lines[2][2:] == ["Canal", "de", "Voz", "do", "Hermes"]


def test_cli_shows_one_board(kanban_home, tmp_path, capsys):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    assert cli.run_board_status(s, "voz-hermes", None) == 0
    assert capsys.readouterr().out.split()[:2] == ["voz-hermes", "Active"]
    assert cli.run_board_status(s, "nope", None) == 1
    assert "board nope not found" in capsys.readouterr().out


@pytest.mark.parametrize("word,expected", [
    ("suspenso", "suspended"), ("suspended", "suspended"), ("concluido", "done"), ("Concluído", "done"),
])
def test_cli_sets_the_status_with_pt_or_en_words(kanban_home, tmp_path, capsys, word, expected):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    assert cli.run_board_status(s, "voz-hermes", word) == 0
    out = capsys.readouterr().out
    st = _cli_store(s)
    assert st.board_status("voz-hermes") == expected
    (act,) = st.recent_actions(["board_status"])
    assert act["detail"] == f"cli: active->{expected}"
    st.close()
    label = {"suspended": "Suspended", "done": "Done"}[expected]
    assert f"voz-hermes: Active → {label}" in out
    assert "New cards on this board will not be started" in out


def test_cli_reactivation_and_unchanged(kanban_home, tmp_path, capsys):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    cli.run_board_status(s, "voz-hermes", "suspenso")
    capsys.readouterr()
    assert cli.run_board_status(s, "voz-hermes", "ativo") == 0
    out = capsys.readouterr().out
    assert "voz-hermes: Suspended → Active" in out and "reminders restart from now" in out
    assert cli.run_board_status(s, "voz-hermes", "active") == 0
    assert "voz-hermes is already Active" in capsys.readouterr().out


def test_cli_rejects_a_bad_status_or_board(kanban_home, tmp_path, capsys):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    assert cli.run_board_status(s, "voz-hermes", "paused") == 2
    assert "unknown status paused" in capsys.readouterr().out
    assert cli.run_board_status(s, "nope", "suspenso") == 1
    assert "board nope not found" in capsys.readouterr().out
    st = _cli_store(s)
    assert st.board_statuses() == {}
    st.close()


def test_cli_in_pt_br(kanban_home, tmp_path, capsys, pt_br):  # noqa: F811
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    assert cli.run_board_status(s, "voz-hermes", "suspenso") == 0
    out = capsys.readouterr().out
    assert "voz-hermes: Ativo → Suspenso" in out
    assert "Novos cards deste quadro não serão iniciados" in out
    cli.run_board_status(s, None, None)
    assert "Suspenso" in capsys.readouterr().out


def test_cli_parser_wires_board_status(kanban_home, tmp_path, capsys):
    import argparse

    from ke import cli

    _two_boards()
    cli.bind(_cli_settings(tmp_path))
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    try:
        assert cli.run(parser.parse_args(["board-status", "portal", "concluido"])) == 0
        assert "portal: Active → Done" in capsys.readouterr().out
        assert cli.run(parser.parse_args(["board-status"])) == 0
        assert "portal" in capsys.readouterr().out
    finally:
        cli.bind(None)


def _doctor(capsys):
    lines = capsys.readouterr().out.strip().splitlines()
    return dict(line.split(": ", 1) for line in lines)


def test_doctor_reports_the_suspended_boards(kanban_home, tmp_path, capsys):
    from ke import cli

    _two_boards()
    s = _cli_settings(tmp_path)
    cli.run_doctor(s)
    assert _doctor(capsys)["boards_suspended"] == "none"
    assert not (s.shared_state_dir / "state.db").exists()  # doctor opens no Store
    st = _cli_store(s)
    st.set_board_status("voz-hermes", "suspended", 1, "cli")
    st.set_board_status("portal", "done", 1, "cli")
    st.set_board_status("gone", "suspended", 1, "cli")  # not a live board
    st.close()
    cli.run_doctor(s)
    assert _doctor(capsys)["boards_suspended"] == "portal (Done), voz-hermes"


def test_doctor_suspended_boards_in_pt_br(kanban_home, tmp_path, capsys, pt_br):  # noqa: F811
    from ke import cli

    s = _cli_settings(tmp_path)
    cli.run_doctor(s)
    assert _doctor(capsys)["boards_suspended"] == "nenhum"


# -- Desktop half (static checks; the lint/validate checks live in test_desktop_plugin.py) -------------
def _js():
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "desktop" / "plugin.js").read_text(encoding="utf-8")


def test_desktop_shows_the_suspended_section_and_status_tags():
    src = _js()
    assert "data.suspended" in src and "t('ui.section.suspended')" in src
    assert "storageGet('suspendedOpen', false)" in src  # collapsed unless the viewer opened it
    tags = src.count("jsx(StatusTag")
    assert tags >= 4, tags  # needs-you card, infra card, running row, drawer header
    assert "t('ui.status.' + status)" in src
    assert "t('ui.needs.reminders_paused')" in src


def test_desktop_projects_panel_posts_the_status_and_refreshes():
    src = _js()
    block = src[src.index("function Projects("):][:4000]
    assert "api('/board-status', { method: 'POST', body: { board, status } })" in block
    assert "invalidateQueries({ queryKey: [ID, 'overview'] })" in block
    assert "t('ui.projects.note')" in block
    assert "STATUSES.map" in block
    assert "boards_status" in src and "t('ui.projects')" in src
    assert "const STATUSES = ['active', 'suspended', 'done']" in src


def test_every_ui_key_the_desktop_uses_is_in_the_catalog():
    import re

    from ke import messages

    src = _js()
    used = set(re.findall(r"\bt\('(ui\.[A-Za-z0-9_.]+)'", src))
    used |= {"ui.status." + s for s in ("active", "suspended", "done")}
    missing = sorted(k for k in used if k not in messages.EN and not k.endswith("."))
    assert not missing, missing


# -- review Minor 5: one failing board does not stop the scan -----------------------------------------
def test_one_failing_board_does_not_stop_the_scan(tmp_path):
    _statuses(tmp_path, a="suspended", b="suspended")
    clock = Tick()
    calls = []

    def locate(board, task_id, *_rest):
        calls.append(board)
        if board == "a":
            raise sqlite3.OperationalError("database is locked")
        return task_id == "t_b" and board == "b"

    from ke.board_status import make_pre_claim_hook

    hook = make_pre_claim_hook(_reader(tmp_path, clock), locate=locate, boards=lambda: ["a", "b"], clock=clock)
    assert hook(task_id="t_b", board="default") == SUSPENDED_EN
    assert hook(task_id="t_x", board="default") is None  # fail-open for a card we could not place
    n = len(calls)
    assert hook(task_id="t_x", board="default") is None
    assert len(calls) > n  # a miss with a failed board is not cached: board a may hold the card
