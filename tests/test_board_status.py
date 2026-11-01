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


def test_hook_refuses_suspended_and_done_boards_and_passes_active_or_unknown(tmp_path):
    from ke.board_status import make_pre_claim_hook

    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.set_board_status("finished", "done", 1, "cli")
    s.set_board_status("back", "suspended", 1, "cli")
    s.set_board_status("back", "active", 2, "cli")
    hook = make_pre_claim_hook(_reader(tmp_path))
    kw = {"assignee": "dev", "workspace_path": None, "claimer": "host:1", "telemetry_schema_version": 1}
    assert hook(task_id="t_1", board="frozen", **kw) == {
        "refuse": "Project suspended in Kanban Empowered — reactivate it to continue."
    }
    assert hook(task_id="t_1", board="finished", **kw) == {
        "refuse": "Project marked done in Kanban Empowered — reactivate it to continue."
    }
    assert hook(task_id="t_1", board="back", **kw) is None
    assert hook(task_id="t_1", board="never-seen", **kw) is None
    assert hook(task_id="t_1", board=None, **kw) is None
    s.close()


def test_hook_reason_in_pt_br(tmp_path, pt_br):  # noqa: F811
    from ke.board_status import make_pre_claim_hook

    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.set_board_status("finished", "done", 1, "cli")
    hook = make_pre_claim_hook(_reader(tmp_path))
    assert hook(task_id="t", board="frozen") == {
        "refuse": "Projeto suspenso no Kanban Empowered — reative para continuar."
    }
    assert hook(task_id="t", board="finished") == {
        "refuse": "Projeto concluído no Kanban Empowered — reative para continuar."
    }
    s.close()


def test_hook_fails_open_on_store_error(tmp_path):
    from ke.board_status import make_pre_claim_hook

    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a sqlite database" * 100)
    assert make_pre_claim_hook(_reader(tmp_path))(task_id="t", board="b") is None

    class Broken:
        def status(self, board):
            raise RuntimeError("boom")

    assert make_pre_claim_hook(Broken())(task_id="t", board="b") is None


def test_hook_fails_open_when_the_store_is_locked(tmp_path):
    """A writer holding an exclusive lock: the read gives up after its short busy timeout, fail-open."""
    from ke.board_status import StatusReader, make_pre_claim_hook

    s = _store(tmp_path)
    s.set_board_status("frozen", "suspended", 1, "cli")
    s.conn.execute("PRAGMA journal_mode=DELETE")  # WAL readers never block; a rollback journal does
    s.conn.execute("BEGIN EXCLUSIVE")
    try:
        reader = StatusReader(tmp_path / "state" / "state.db", busy_ms=50, clock=Tick())
        started = time.monotonic()
        assert make_pre_claim_hook(reader)(task_id="t", board="frozen") is None
        assert time.monotonic() - started < 2.0
    finally:
        s.conn.execute("ROLLBACK")
        s.close()


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


@pytest.fixture
def worker_profile(monkeypatch):
    """A worker process: not the coordinator profile, so only the profile-wide hooks are registered."""
    from ke import hermes_api

    monkeypatch.setattr(hermes_api, "active_profile", lambda: "worker-x")


def test_register_subscribes_pre_claim_in_any_profile_when_the_hooks_exist(
    load_plugin, kanban_home, tmp_path, monkeypatch
):
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
    out = cb(task_id="t_1", board="frozen", assignee="dev", workspace_path=None, claimer="h", telemetry_schema_version=1)
    assert isinstance(out, dict) and out["refuse"]
    assert cb(task_id="t_1", board="default", assignee="dev", workspace_path=None, claimer="h") is None


def test_register_skips_pre_claim_without_the_veto_hooks(load_plugin, kanban_home, tmp_path, monkeypatch):
    mod = load_plugin()
    api = __import__(mod.__name__ + ".ke.hermes_api", fromlist=["x"])
    monkeypatch.setattr(api, "active_profile", lambda: "worker-x")
    monkeypatch.setattr(api, "veto_hooks_available", lambda: False)
    ctx = _Ctx({"shared_state_dir": str(tmp_path / "state")})
    mod.register(ctx)
    assert "kanban_pre_claim" not in ctx.hooks
    assert "pre_tool_call" in ctx.hooks


def test_hook_refusal_keeps_a_real_card_ready(kanban_home, tmp_path):
    """End to end against the installed Hermes: a subscribed hook refusing keeps the card 'ready'."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import plugins as hp
    from ke import hermes_api
    from ke.board_status import StatusReader, make_pre_claim_hook

    if not hermes_api.veto_hooks_available():
        pytest.skip("this Hermes build has no veto hooks")
    from conftest import create_task

    s = _store(tmp_path)
    s.set_board_status("default", "suspended", 1, "cli")
    s.close()
    hook = make_pre_claim_hook(StatusReader(tmp_path / "state" / "state.db"))
    manager = hp.get_plugin_manager()
    manager._hooks.setdefault("kanban_pre_claim", []).append(hook)
    try:
        with hermes_api.connect("default") as conn:
            tid = create_task(conn, title="frozen work", assignee="dev")
            assert kb.claim_task(conn, tid) is None
            assert hermes_api.task(conn, tid).status == "ready"
            refused = [e for e in hermes_api.events(conn, tid) if e.kind == "claim_refused"]
            assert refused and "Kanban Empowered" in str(refused[-1].payload)
    finally:
        manager._hooks["kanban_pre_claim"].remove(hook)
