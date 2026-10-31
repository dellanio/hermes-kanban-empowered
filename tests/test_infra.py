"""Task 11: conservative infrastructure-failure recovery with a budget."""
from __future__ import annotations

import os
import time

import pytest

from conftest import FakeTelegram, create_task, make_coordinator, settings_for_test
from test_conditions import _raw_event
from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)

OWNER_CHAT = 6802889854
INFRA_ERR = "Kanban execution unknown; retained by owner"
CONTENT_ERR = "AssertionError in tests"
DEFAULT = (
    "Kanban execution unknown",
    "retained by owner",
    "Gateway connection failed",
    "WorkerPersistenceError",
)


# -- helpers -------------------------------------------------------------------
class Api:
    """The real adapter with the coordinator-facing bits pinned (no real profiles / Hermes checks)."""

    def __init__(self):
        from ke import hermes_api

        self._api = hermes_api

    def active_profile(self):
        return "default"

    def veto_hooks_available(self):
        return True

    def __getattr__(self, name):
        return getattr(self._api, name)


def _store(tmp_path):
    from ke.store import Store

    return Store(tmp_path / "state" / "state.db")


def _ir(tmp_path, clock, api=None, **overrides):
    from ke.infra import InfraRecovery

    settings = settings_for_test(tmp_path, **overrides)
    store = _store(tmp_path)
    return InfraRecovery(settings, store, api or Api(), clock=clock), store


def _fail_run(conn, tid, error, exit_kind="nonzero_exit", outcome="crashed"):
    """One attempt that ends in a crash the way Hermes' reclaim books it: run closed, card back to ready."""
    from hermes_cli import kanban_db as kb

    assert kb.claim_task(conn, tid) is not None
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome=outcome, status=outcome, error=error, metadata={"exit_kind": exit_kind})
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (tid,),
        )


def gave_up_with_runs(kinds, board="default", tid=None, live_pid=False, errors=None):
    """Run ``kinds`` ("infra"/"content"/...) as failed runs of one card, then trip Hermes' breaker.

    Returns the card id. ``live_pid`` leaves a live worker pid on the (blocked) card.
    """
    from ke import hermes_api
    from hermes_cli import kanban_db_dispatch as kd

    errors = {"infra": INFRA_ERR, "content": CONTENT_ERR, **(errors or {})}
    with hermes_api.connect(board) as conn:
        if tid is None:
            tid = create_task(conn, title="card", assignee="dev")
        for kind in kinds:
            _fail_run(conn, tid, errors[kind])
        assert kd._record_task_failure(
            conn, tid, error=errors[kinds[-1]], outcome="crashed", failure_limit=1,
            release_claim=False, end_run=False, event_payload_extra={"pid": 4242},
        )
        if live_pid:
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
            conn.commit()
    return tid


def _task(tid, board="default"):
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        return hermes_api.task(conn, tid)


def _status(tid, board="default"):
    return _task(tid, board).status


def _comments(tid, board="default"):
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        return [c.body for c in hermes_api.comments(conn, tid)]


def _undo_count(store, tid, board="default"):
    return store.infra_undos_since(board, tid, 0)


def _sent(store, chat=OWNER_CHAT):
    return [
        r for r in store.conn.execute("SELECT * FROM sent_messages WHERE kind = 'infra'").fetchall()
        if r["chat_id"] == chat
    ]


# -- classify_exit -----------------------------------------------------------------
def test_classify():
    from ke.infra import classify_exit

    assert classify_exit("Kanban execution unknown; retained by owner", "nonzero_exit", DEFAULT) == "infra"
    assert classify_exit(None, "rate_limited", DEFAULT) == "quota"
    assert classify_exit("AssertionError in tests", "nonzero_exit", DEFAULT) == "content"


def test_classify_every_default_pattern_and_case_insensitive():
    from ke.infra import classify_exit

    for pattern in DEFAULT:
        assert classify_exit(f"boom: {pattern} (x)", None, DEFAULT) == "infra"
    assert classify_exit("GATEWAY CONNECTION FAILED", None, DEFAULT) == "infra"


def test_classify_unknown_and_quota_wins_over_a_pattern():
    from ke.infra import classify_exit

    assert classify_exit(None, "nonzero_exit", DEFAULT) == "unknown"
    assert classify_exit("   ", None, DEFAULT) == "unknown"
    assert classify_exit(INFRA_ERR, "rate_limited", DEFAULT) == "quota"
    assert classify_exit(INFRA_ERR, "nonzero_exit", ()) == "content"  # custom (empty) patterns


# -- evaluate: when it undoes ----------------------------------------------------------
def test_undo_when_all_failures_are_infra(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    assert _status(tid) == "blocked"
    ir.evaluate(clock.now)
    assert _status(tid) == "ready"
    assert _undo_count(store, tid) == 1
    (comment,) = [c for c in _comments(tid) if "infrastructure" in c]
    assert comment.startswith("kanban-empowered: ")
    assert "2 infrastructure failures" in comment and "Kanban execution unknown" in comment
    (action,) = [a for a in store.actions_since(0) if a["kind"] == "infra_undo"]
    assert (action["board"], action["task_id"], action["ts"]) == ("default", tid, int(clock.now))


def test_undo_only_when_all_failures_infra(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "content"])
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"
    assert _undo_count(store, tid) == 0
    tid2 = gave_up_with_runs(["content", "infra"])
    ir.evaluate(clock.now)
    assert _status(tid2) == "blocked"


def test_unknown_error_and_quota_runs_are_never_undone(kanban_home, tmp_path, clock):
    ir, _ = _ir(tmp_path, clock)
    silent = gave_up_with_runs(["infra", "x"], errors={"x": ""})
    quota = gave_up_with_runs(["infra", "x"], errors={"x": "pid 1 exited rate-limited"})
    ir.evaluate(clock.now)
    assert _status(silent) == "blocked" and _status(quota) == "blocked"


def test_only_failures_since_the_last_completed_run_count(kanban_home, tmp_path, clock):
    from ke import hermes_api
    from hermes_cli import kanban_db as kb

    ir, _ = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="card", assignee="dev")
        _fail_run(conn, tid, CONTENT_ERR)  # an old content failure ...
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary="ok", expected_run_id=run_id)  # ... then a success
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
    gave_up_with_runs(["infra", "infra"], tid=tid)
    ir.evaluate(clock.now)
    assert _status(tid) == "ready"


def test_live_worker_blocks_undo(kanban_home, tmp_path, clock, monkeypatch):
    ir, store = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"], live_pid=True)
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked" and _undo_count(store, tid) == 0


def test_live_pid_is_decided_by_the_liveness_probe(kanban_home, tmp_path, clock, monkeypatch):
    from ke import infra

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"], live_pid=True)
    monkeypatch.setattr(infra, "pid_alive", lambda pid: False)  # the worker is gone: nothing executes
    ir.evaluate(clock.now)
    assert _status(tid) == "ready"


def test_an_unfinished_run_blocks_undo(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    with hermes_api.connect("default") as conn:
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)", (tid, int(time.time()))
        )
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (cur.lastrowid, tid))
        conn.commit()
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


def test_a_dangling_current_run_blocks_undo(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    with hermes_api.connect("default") as conn:
        conn.execute("UPDATE tasks SET current_run_id = 987654 WHERE id = ?", (tid,))
        conn.commit()
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


# -- evaluate: what it must never touch ---------------------------------------------------
def test_human_block_is_never_undone(kanban_home, tmp_path, clock):
    from ke import hermes_api
    from hermes_cli import kanban_db as kb

    ir, store = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="needs a person", assignee="dev")
        _fail_run(conn, tid, INFRA_ERR)
        _fail_run(conn, tid, INFRA_ERR)
        assert kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="plug the board in", kind="needs_input")
    assert _status(tid) == "blocked"
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked" and _undo_count(store, tid) == 0


def test_a_typed_breaker_block_is_never_undone(kanban_home, tmp_path, clock):
    from ke import hermes_api
    from hermes_cli import kanban_db as kb

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    with hermes_api.connect("default") as conn:
        assert kb.block_task(conn, tid, reason="a human classified it", kind="needs_input")  # in place
        assert hermes_api.task(conn, tid).block_kind == "needs_input"
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


def test_stale_block_kind_from_an_earlier_human_block_is_never_undone(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    with hermes_api.connect("default") as conn:
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (tid,))  # kept across an unblock
        conn.commit()
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


def test_later_event_means_the_gave_up_is_stale(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    with hermes_api.connect("default") as conn:
        _raw_event(conn, tid, "assigned", {"assignee": "ops"})  # somebody touched the card after the breaker
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


def test_a_card_blocked_without_a_gave_up_is_ignored(kanban_home, tmp_path, clock):
    from ke import hermes_api
    from hermes_cli import kanban_db as kb

    ir, _ = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, INFRA_ERR)
        assert kb.block_task(conn, tid, reason="old style block")  # untyped, but the worker's own
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


@pytest.mark.parametrize("flag", ["sticky", "terminal_provider", "protocol_violation"])
def test_terminal_provider_and_protocol_violation_trips_are_never_undone(kanban_home, tmp_path, clock, flag):
    from ke import hermes_api
    from hermes_cli import kanban_db_dispatch as kd

    ir, _ = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, INFRA_ERR)
        assert kd._record_task_failure(
            conn, tid, error=INFRA_ERR, outcome="crashed", force_trip=True, release_claim=False,
            end_run=False, event_payload_extra={flag: True},
        )
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"


def test_force_tripped_sticky_block_is_never_undone(kanban_home, tmp_path, clock):
    """Hermes holds a sticky gave_up (force_trip, systemic same-error burst) for an operator."""
    from ke import hermes_api
    from hermes_cli import kanban_db_dispatch as kd

    ir, store = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, INFRA_ERR)
        _fail_run(conn, tid, INFRA_ERR)
        assert kd._record_task_failure(
            conn, tid, error=INFRA_ERR, outcome="crashed", force_trip=True, release_claim=False, end_run=False,
        )
        events = hermes_api.events(conn, tid)
        assert events[-1].kind == "gave_up" and events[-1].payload.get("sticky") is True
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked" and _undo_count(store, tid) == 0


def test_overlapping_coordinators_cannot_both_spend_the_last_unit(kanban_home, tmp_path, clock):
    """Another coordinator spends the budget between our check and our insert: we give our row back."""
    ir, store = _ir(tmp_path, clock, infra_undo_count=1)
    tid = gave_up_with_runs(["infra", "infra"])
    real = store.add_infra_undo

    def racing(board, task_id, ts):
        store.log_action(board, task_id, "x", None, ts)  # (no-op marker)
        real(board, task_id, ts)  # the other coordinator's undo lands first
        return real(board, task_id, ts)

    store.add_infra_undo = racing
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked"
    assert _undo_count(store, tid) == 1  # only the other coordinator's row remains


def test_version_changes_between_the_check_and_the_unblock_abort_the_undo(kanban_home, tmp_path, clock):
    """A human re-blocks / touches the card after the assessment: the unblock must not happen."""
    from ke import hermes_api

    class Racy(Api):
        def __init__(self, tid):
            super().__init__()
            self.tid = tid
            self.calls = 0

        def events(self, conn, tid):
            out = self._api.events(conn, tid)
            self.calls += 1
            if tid == self.tid and self.calls >= 2:  # the re-check sees a newer event
                _raw_event(conn, tid, "assigned", {"assignee": "ops"})
                out = self._api.events(conn, tid)
            return out

    tid = gave_up_with_runs(["infra", "infra"])
    ir, store = _ir(tmp_path, clock, api=Racy(tid))
    ir.evaluate(clock.now)
    assert _status(tid) == "blocked" and _undo_count(store, tid) == 0


def test_pattern_setting_is_honored(kanban_home, tmp_path, clock):
    ir, _ = _ir(tmp_path, clock, infra_patterns=("disk on fire",))
    mine = gave_up_with_runs(["a", "a"], errors={"a": "the disk on fire again"})
    stock = gave_up_with_runs(["infra", "infra"])  # the stock patterns no longer count
    ir.evaluate(clock.now)
    assert _status(mine) == "ready" and _status(stock) == "blocked"


# -- budget ---------------------------------------------------------------------------
def test_budget_two_per_24h(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT}
    )
    tid = None
    for _ in range(3):
        tid = gave_up_with_runs(["infra", "infra"], tid=tid)
        co.evaluate(clock.now)
        clock.advance(60)
    assert _undo_count(co.store, tid) == 2
    assert _status(tid) == "blocked"
    assert len(_sent(co.store)) == 1 and fake_tg.count("send") == 1
    assert tid in fake_tg.last_text


def test_budget_exhausted_alert_is_not_repeated_nor_a_retry(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT},
        infra_undo_count=1,
    )
    tid = gave_up_with_runs(["infra", "infra"])
    co.evaluate(clock.now)  # undo #1
    gave_up_with_runs(["infra"], tid=tid)
    for _ in range(5):  # budget exhausted: alert once, never undo
        co.evaluate(clock.now)
        clock.advance(300)
    assert _undo_count(co.store, tid) == 1 and _status(tid) == "blocked"
    assert fake_tg.count("send") == 1


def test_budget_window_slides_and_the_alert_is_per_window(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT},
        infra_undo_count=1,
    )
    tid = gave_up_with_runs(["infra", "infra"])
    co.evaluate(clock.now)  # undo #1 at t0
    gave_up_with_runs(["infra"], tid=tid)
    co.evaluate(clock.now)  # exhausted: alert #1
    assert fake_tg.count("send") == 1
    clock.advance(86400 - 1)
    co.evaluate(clock.now)  # still inside the window: no undo
    assert _status(tid) == "blocked"
    clock.advance(2)  # the old undo left the window
    co.evaluate(clock.now)
    assert _status(tid) == "ready" and _undo_count(co.store, tid) == 2
    gave_up_with_runs(["infra"], tid=tid)
    co.evaluate(clock.now)  # exhausted again, in a new window: alert #2
    assert fake_tg.count("send") == 2


def test_budget_is_per_card(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock, infra_undo_count=1)
    a = gave_up_with_runs(["infra", "infra"])
    b = gave_up_with_runs(["infra", "infra"])
    ir.evaluate(clock.now)
    assert _status(a) == "ready" and _status(b) == "ready"


def test_zero_budget_never_undoes_but_alerts(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock, infra_undo_count=0)
    tid = gave_up_with_runs(["infra"])
    alerts = ir.evaluate(clock.now)
    assert _status(tid) == "blocked"
    assert [(a["kind"], a["task_id"]) for a in alerts] == [("budget", tid)]


def test_budget_alert_goes_to_the_board_owner(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(),
        owner_ids={"sponsor": OWNER_CHAT, "ana": 77}, board_owner={"default": "ana"}, infra_undo_count=0,
    )
    gave_up_with_runs(["infra"])
    co.evaluate(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [77]


def test_failed_alert_send_is_retried_and_not_marked(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT},
        infra_undo_count=0,
    )
    gave_up_with_runs(["infra"])
    fake_tg.fail_next = OSError("network down")
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 0 and _sent(co.store) == []
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 1 and len(_sent(co.store)) == 1


def test_no_telegram_does_not_break_the_step(kanban_home, tmp_path, clock):
    co = make_coordinator(tmp_path, clock=clock, telegram=None, api=Api(), infra_undo_count=0)
    gave_up_with_runs(["infra"])
    co.evaluate(clock.now)  # warns once, nothing raises


# -- record_exit / hook / systemic ------------------------------------------------------------
def test_record_exit_logs_an_infra_exit_action(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock)
    ir.record_exit("default", "t_1", 7, "infra", clock.now)
    (row,) = [a for a in store.actions_since(0) if a["kind"] == "infra_exit"]
    assert (row["board"], row["task_id"], row["ts"]) == ("default", "t_1", int(clock.now))
    assert "infra" in row["detail"] and "7" in row["detail"]


def test_record_exit_is_idempotent_per_run(kanban_home, tmp_path, clock):
    ir, store = _ir(tmp_path, clock)
    ir.record_exit("default", "t_1", 7, "infra", clock.now)
    ir.record_exit("default", "t_1", 7, "infra", clock.now + 5)
    assert len([a for a in store.actions_since(0) if a["kind"] == "infra_exit"]) == 1


def test_worker_exited_hook_classifies_by_the_run_error(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, store = _ir(tmp_path, clock)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, INFRA_ERR)
        run_id = hermes_api.runs(conn, tid)[-1].id
    ir.on_worker_exited(
        task_id=tid, board="default", run_id=run_id, profile_name="dev", assignee="dev", worker_pid=1,
        exit_kind="nonzero_exit", exit_code=1, outcome="crashed", retry_status="ready",
    )
    (row,) = [a for a in store.actions_since(0) if a["kind"] == "infra_exit"]
    assert row["task_id"] == tid and row["detail"].startswith("infra")


def test_worker_exited_hook_records_quota_and_content_but_they_do_not_count_as_infra(kanban_home, tmp_path, clock):
    from ke import hermes_api

    ir, store = _ir(tmp_path, clock, infra_alert_count=1)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, CONTENT_ERR)
        run_id = hermes_api.runs(conn, tid)[-1].id
    ir.on_worker_exited(task_id=tid, board="default", run_id=run_id, exit_kind="nonzero_exit")
    ir.on_worker_exited(task_id="t_other", board="default", run_id=None, exit_kind="rate_limited")
    kinds = sorted(a["detail"].split(":")[0] for a in store.actions_since(0) if a["kind"] == "infra_exit")
    assert kinds == ["content", "quota"]
    assert ir.evaluate(clock.now) == []  # neither is an infrastructure exit


def test_worker_exited_hook_never_raises(kanban_home, tmp_path, clock):
    class Boom(Api):
        def connect(self, board):
            raise RuntimeError("db locked")

    ir, store = _ir(tmp_path, clock, api=Boom())
    assert ir.on_worker_exited(task_id="t_1", board="default", run_id=3, exit_kind="nonzero_exit") is None
    assert ir.on_worker_exited() is None
    assert ir.on_worker_exited(task_id=None, board=None, run_id="x", exit_kind=object(), junk=1) is None
    store.close()  # even a dead store must not leak an exception into the dispatcher
    assert ir.on_worker_exited(task_id="t_1", board="default", run_id=3, exit_kind="rate_limited") is None


def test_systemic_alert_once_per_hour(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT}
    )
    ir = co.infra
    ir.record_exit("default", "t_1", 1, "infra", clock.now)
    ir.record_exit("default", "t_2", 2, "infra", clock.now)
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 0  # two is below the limit
    ir.record_exit("default", "t_3", 3, "infra", clock.now)
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 1
    assert "3" in fake_tg.last_text
    clock.advance(1800)
    ir.record_exit("default", "t_4", 4, "infra", clock.now)
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 1  # once per hour
    clock.advance(3601)  # the first three left the window, the fourth is alone
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 1
    for n in (5, 6, 7):
        ir.record_exit("default", f"t_{n}", n, "infra", clock.now)
    co.evaluate(clock.now)
    assert fake_tg.count("send") == 2  # a new hour, a new alert
    assert len(_sent(co.store)) == 2


def test_systemic_alert_goes_to_the_default_owner(kanban_home, tmp_path, clock, fake_tg):
    co = make_coordinator(
        tmp_path, clock=clock, telegram=fake_tg, api=Api(), owner_ids={"sponsor": OWNER_CHAT, "ana": 77},
        board_owner={"default": "ana"}, infra_alert_count=1,
    )
    co.infra.record_exit("default", "t_1", 1, "infra", clock.now)
    co.evaluate(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [OWNER_CHAT]


def test_systemic_alert_settings_are_honored(kanban_home, tmp_path, clock):
    ir, _ = _ir(tmp_path, clock, infra_alert_count=2, infra_alert_window_s=60)
    ir.record_exit("default", "t_1", 1, "infra", clock.now)
    clock.advance(61)
    ir.record_exit("default", "t_2", 2, "infra", clock.now)
    assert ir.evaluate(clock.now) == []
    ir.record_exit("default", "t_3", 3, "infra", clock.now)
    assert [a["kind"] for a in ir.evaluate(clock.now)] == ["systemic"]


# -- wiring --------------------------------------------------------------------------------------
def test_coordinator_registers_the_infra_step(kanban_home, tmp_path, clock):
    co = make_coordinator(tmp_path, clock=clock, api=Api())
    assert co.steps["infra"] is not None


def test_infra_step_failure_does_not_stop_the_evaluation(kanban_home, tmp_path, clock, caplog):
    co = make_coordinator(tmp_path, clock=clock, api=Api())

    def boom(now):
        raise RuntimeError("nope")

    co.infra.evaluate = boom
    co.evaluate(clock.now)
    assert "infra" in caplog.text


def test_register_wires_the_exit_hook_only_in_coordinator(kanban_home, tmp_path, load_plugin):
    from test_doctor import _Ctx

    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)
    cb = ctx.hooks["on_kanban_worker_exited"]
    # Hermes passes keywords; whatever arrives, nothing may escape into the dispatcher
    assert cb(
        task_id="t_x", board="default", run_id=1, profile_name="default", assignee="dev", worker_pid=1,
        exit_kind="nonzero_exit", exit_code=1, outcome="crashed", retry_status="ready",
    ) is None
    assert cb() is None

    mod2 = load_plugin("ke_plugin_pkg2")
    ctx2 = _Ctx(tmp_path / "state2", config={"coordinator_profile": "other"})
    mod2.register(ctx2)
    assert "on_kanban_worker_exited" not in ctx2.hooks


def test_exit_hook_feeds_the_coordinator_store(kanban_home, tmp_path, load_plugin):
    from test_doctor import _Ctx
    from ke import hermes_api

    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="x", assignee="dev")
        _fail_run(conn, tid, "Gateway connection failed: timeout")
        run_id = hermes_api.runs(conn, tid)[-1].id
    ctx.hooks["on_kanban_worker_exited"](task_id=tid, board="default", run_id=run_id, exit_kind="nonzero_exit")
    from ke.store import Store

    store = Store(tmp_path / "state" / "state.db")
    assert [a["task_id"] for a in store.actions_since(0) if a["kind"] == "infra_exit"] == [tid]


# -- i18n -------------------------------------------------------------------------------------------
def test_alert_texts_render_in_pt_br(pt_br):
    from ke.messages import render_infra_alert

    budget = render_infra_alert(
        {"kind": "budget", "board": "tw", "task_id": "t_1", "title": "Deploy", "count": 2, "window_s": 86400}
    )
    assert "tw" in budget and "t_1" in budget and "Deploy" in budget
    assert "1 d" in budget  # 86400 s, via human_age
    assert "desfazer" in budget  # translated, not the English fallback
    systemic = render_infra_alert({"kind": "systemic", "n": 3, "window_s": 3600})
    assert "3" in systemic and "1 h" in systemic and "infraestrutura" in systemic


def test_undo_comment_follows_the_language(kanban_home, tmp_path, clock, pt_br):
    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra", "infra"])
    ir.evaluate(clock.now)
    assert any(c.startswith("kanban-empowered: bloqueio desfeito: 2 falhas de infraestrutura") for c in _comments(tid))


def test_one_failure_uses_the_singular(kanban_home, tmp_path, clock):
    ir, _ = _ir(tmp_path, clock)
    tid = gave_up_with_runs(["infra"])
    ir.evaluate(clock.now)
    assert any("1 infrastructure failure (" in c for c in _comments(tid))
