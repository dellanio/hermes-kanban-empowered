import pathlib

import pytest

from conftest import block_task, create_task, make_coordinator, settings_for_test

H = 3600
D = 86400


# -- helpers -------------------------------------------------------------------
def _tracker(tmp_path, clock=None, **overrides):
    from ke import hermes_api
    from ke.store import Store
    from ke.waits import WaitTracker

    settings = settings_for_test(tmp_path, **overrides)
    store = Store(settings.shared_state_dir / "state.db")
    return WaitTracker(settings, store, hermes_api), store, settings


def make_human_block(conn, reason="Preciso: a\nPor quê: b\nComo responder: c", kind="needs_input", **task_kw):
    """Create a task, claim it and block it; returns (task_id, blocked event)."""
    from hermes_cli import kanban_db as kb

    task_kw.setdefault("assignee", "dev")
    tid = create_task(conn, **task_kw)
    assert kb.claim_task(conn, tid) is not None
    assert block_task(conn, tid, reason=reason, kind=kind)
    return tid, _last_blocked(conn, tid)


def _last_blocked(conn, tid):
    from ke import hermes_api

    return max((e for e in hermes_api.events(conn, tid) if e.kind == "blocked"), key=lambda e: e.id)


def _start(clock, ev):
    """Align the fake clock with the (real-time) blocked event."""
    clock.now = float(ev.created_at)


def _stages(dues):
    return [d.stage for d in dues]


# -- stages --------------------------------------------------------------------
def test_stages_follow_spec(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, "Preciso: a\nPor quê: b\nComo responder: c")
    _start(clock, ev)
    wt.sync(clock.now)
    row = store.latest_wait("default", tid)
    assert row["version"] == ev.id and row["started_at"] == ev.created_at and row["owner"] == "sponsor"
    clock.advance(1799)
    assert wt.collect_due(clock.now) == []
    clock.advance(1)
    dues = wt.collect_due(clock.now)
    assert _stages(dues) == ["reminder1"]
    d = dues[0]
    assert (d.board, d.task_id, d.version, d.owner, d.age_s) == ("default", tid, ev.id, "sponsor", 1800)
    assert d.ask is not None and (d.ask.need, d.ask.why, d.ask.reply) == ("a", "b", "c")
    assert d.held_behind == []


def test_full_schedule_with_mark_sent(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)

    def step(advance_to, expected):
        clock.now = ev.created_at + advance_to
        dues = wt.collect_due(clock.now)
        assert _stages(dues) == expected, advance_to
        for d in dues:
            wt.mark_sent(d, clock.now)

    step(1800, ["reminder1"])
    step(1800 + 30, [])  # already sent
    step(7199, [])
    step(7200, ["reminder2"])
    step(28799, [])
    step(28800, ["escalation"])
    step(28800 + D - 1, [])
    step(28800 + D, ["digest"])
    step(28800 + D + 60, [])
    step(28800 + 2 * D - 1, [])
    step(28800 + 2 * D, ["digest"])
    row = store.latest_wait("default", tid)
    assert row["stage"] == "digest" and row["last_sent_at"] == ev.created_at + 28800 + 2 * D


def test_collect_due_does_not_mark_sent(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        _, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    assert _stages(wt.collect_due(clock.now)) == ["reminder1"]
    assert _stages(wt.collect_due(clock.now)) == ["reminder1"]  # still pending until mark_sent


def test_gateway_down_condenses_to_highest_stage(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        _, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(9 * H)
    dues = wt.collect_due(clock.now)
    assert _stages(dues) == ["escalation"]  # one message, no burst


def test_gateway_down_for_days_sends_one_digest(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        _, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(4 * D)
    dues = wt.collect_due(clock.now)
    assert _stages(dues) == ["digest"]
    wt.mark_sent(dues[0], clock.now)
    assert wt.collect_due(clock.now) == []


def test_one_due_per_wait(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        a, ev_a = make_human_block(conn, title="a")
        b, ev_b = make_human_block(conn, title="b")
    _start(clock, max((ev_a, ev_b), key=lambda e: e.created_at))
    wt.sync(clock.now)
    clock.advance(9 * H)
    dues = wt.collect_due(clock.now)
    assert sorted(d.task_id for d in dues) == sorted([a, b]) and _stages(dues) == ["escalation", "escalation"]


# -- sync ----------------------------------------------------------------------
def test_reblock_creates_new_version(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev1 = make_human_block(conn, kind=None)  # untyped counts as human
    _start(clock, ev1)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid)["version"] == ev1.id
    with hermes_api.connect("default") as conn:
        assert hermes_api.unblock(conn, tid)
        assert block_task(conn, tid, reason="Preciso: x\nPor quê: y\nComo responder: z", kind="needs_input")
        ev2 = _last_blocked(conn, tid)
    assert ev2.id != ev1.id
    clock.now = float(ev2.created_at)
    wt.sync(clock.now)
    row = store.latest_wait("default", tid)
    assert row["version"] == ev2.id != ev1.id
    n = store.conn.execute("SELECT COUNT(*) FROM waits WHERE task_id = ?", (tid,)).fetchone()[0]
    assert n == 1  # the stale version is gone


def test_sync_is_idempotent_and_keeps_sent_state(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    (due,) = wt.collect_due(clock.now)
    wt.mark_sent(due, clock.now)
    wt.sync(clock.now + 5)
    wt.sync(clock.now + 10)
    row = store.latest_wait("default", tid)
    assert row["stage"] == "reminder1" and row["last_sent_at"] == int(clock.now)
    assert wt.collect_due(clock.now + 10) == []


def test_unblocked_card_is_removed(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is not None
    with hermes_api.connect("default") as conn:
        hermes_api.unblock(conn, tid)
    wt.sync(clock.now + 60)
    assert store.latest_wait("default", tid) is None
    clock.advance(9 * H)
    assert wt.collect_due(clock.now) == []


def test_non_human_kinds_are_not_tracked(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, kind="transient")
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is None


def test_capability_block_waits_on_a_person_by_default(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, kind="capability")
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid)["version"] == ev.id


def test_human_kinds_setting_is_honored(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path, human_kinds=("needs_input",))
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, kind="capability")
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is None


def test_owner_comes_from_board_config(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path, default_owner="sponsor", board_owner={"default": "ana"})
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid)["owner"] == "ana"
    clock.advance(1800)
    assert wt.collect_due(clock.now)[0].owner == "ana"


def test_unreadable_board_keeps_its_rows(kanban_home, clock, tmp_path, monkeypatch):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)

    def boom(board):
        raise RuntimeError("db locked")

    monkeypatch.setattr(hermes_api, "connect", boom)
    wt.sync(clock.now + 60)  # must not raise nor forget the wait
    assert store.latest_wait("default", tid) is not None


def test_wait_for_a_card_that_is_gone_is_dropped(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    store.conn.execute(
        "INSERT INTO waits (board, task_id, version, owner, started_at) VALUES ('default','t_ghost',1,'sponsor',?)",
        (int(clock.now),),
    )
    wt.sync(clock.now)
    assert store.latest_wait("default", "t_ghost") is None


# -- ask / held_behind ---------------------------------------------------------
def test_raw_reason_without_structured_ask(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        _, ev = make_human_block(conn, reason="aguardando o sponsor")
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    (due,) = wt.collect_due(clock.now)
    assert due.ask is None and due.reason == "aguardando o sponsor"


def test_held_behind_lists_todo_descendants_bfs(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, _, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
        c1 = create_task(conn, title="c1", parents=[tid])
        c2 = create_task(conn, title="c2", parents=[tid])
        g1 = create_task(conn, title="g1", parents=[c1])
        both = create_task(conn, title="both", parents=[c1, c2])  # diamond
        ready = create_task(conn, title="ready elsewhere", assignee="dev")
        assert hermes_api.task(conn, c1).status == "todo"
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    (due,) = wt.collect_due(clock.now)
    assert sorted(due.held_behind[:2]) == sorted([c1, c2])  # BFS: direct children first
    assert sorted(due.held_behind) == sorted([c1, c2, g1, both])
    assert len(due.held_behind) == 4 and ready not in due.held_behind


# -- messages ------------------------------------------------------------------
def _due(task_id="t_1", board="voz", owner="sponsor", stage="reminder2", age_s=7200, ask=True, held=(), reason=""):
    from ke.asks import Ask
    from ke.waits import Due

    return Due(
        board=board,
        task_id=task_id,
        version=7,
        owner=owner,
        stage=stage,
        age_s=age_s,
        ask=Ask("ligue a placa", "o teste so roda com ela", "responda feito") if ask else None,
        held_behind=list(held),
        reason=reason,
    )


def test_render_single_item_follows_spec_format():
    # message format v2: header, title, status, action, reply word, context; no ids, no number
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due("t_02690416", held=["t_cbdccb7a", "t_577bd508"])])
    lines = txt.splitlines()
    assert lines[:5] == ["📋 KANBAN · NEEDS YOU", "1 pending · 2 blocked cards", "", "VOZ",
                         "⏳ Stalled for 2 hours · blocks 2 cards"]
    assert "👉 ligue a placa" in txt and "o teste so roda com ela" in txt
    assert "Then reply: “feito”." in txt
    assert "t_" not in txt and "hermes kanban-empowered" not in txt and "①" not in txt


def test_render_omits_held_line_when_nothing_is_held():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due(held=[])])
    assert "⏳ Stalled for 2 hours" in txt.splitlines() and "blocks" not in txt
    # header (2) + blank, title, status, blank, action, reply word, blank, why
    assert len(txt.splitlines()) == 10


def test_render_numbers_items_when_grouped():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due("t_a", board="b1"), _due("t_b", board="b2", held=["t_x"])])
    assert "① B1" in txt and "② B2" in txt and "-" * 38 in txt
    assert txt.splitlines()[-3:] == ["↩️ When replying, name the item:", "“1 — feito”", "“2 — feito”"]
    assert "t_a" not in txt and "t_x" not in txt


def test_render_raw_reason_without_ask():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due(ask=False, reason="aguardando a placa")])
    assert "👉 aguardando a placa" in txt and "Reply to this message when it's done, or say what's missing." in txt and "t_1" not in txt


def test_render_has_no_stage_marker():
    # "urgent" only with a concrete reason: v0.1 has no deadlines, so every stage looks the same
    from ke.messages import render_waits

    reminder = render_waits("sponsor", [_due(stage="reminder1")]).splitlines()
    escalation = render_waits("sponsor", [_due(stage="escalation", age_s=8 * H)]).splitlines()
    digest = render_waits("sponsor", [_due(stage="digest", age_s=32 * H)]).splitlines()
    assert reminder[0] == escalation[0] == digest[0] == "📋 KANBAN · NEEDS YOU"
    assert "⏳ Stalled for 8 hours" in escalation and "⏳ Stalled for 1 day" in digest


@pytest.fixture
def pt_br(kanban_home, monkeypatch):
    from agent import i18n, i18n_layers

    root = pathlib.Path(__file__).resolve().parent.parent / "locales"
    entries = [
        i18n_layers.register_pack("en", "core", i18n_layers.parse_locale_file(root / "en.yaml"), source="test"),
        i18n_layers.register_pack("pt-br", "core", i18n_layers.parse_locale_file(root / "pt-br.yaml"), source="test"),
    ]
    monkeypatch.setenv("HERMES_LANGUAGE", "pt-br")
    i18n.reset_language_cache()
    yield
    for e in entries:
        i18n_layers.unregister_pack(e)
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    i18n.reset_language_cache()


def test_render_in_pt_br(pt_br):
    from ke.messages import render_waits

    single = render_waits("sponsor", [_due("t_02690416", held=["t_a", "t_b"])])
    lines = single.splitlines()
    assert lines[:2] == ["📋 KANBAN · PRECISA DE VOCÊ", "1 pendência · 2 cards bloqueados"]
    assert "⏳ Parado há 2 horas · bloqueia 2 cards" in lines
    assert "👉 ligue a placa" in lines and "Depois, responda: “feito”." in lines
    assert lines[-1] == "o teste so roda com ela"
    grouped = render_waits("sponsor", [_due("t_a"), _due("t_b")])
    assert "① VOZ" in grouped and "② VOZ" in grouped
    assert grouped.splitlines()[-3:] == ["↩️ Ao responder, indique o item:", "“1 — feito”", "“2 — feito”"]


# -- coordinator wiring --------------------------------------------------------
def test_coordinator_waits_step_syncs_and_keeps_dues(kanban_home, clock, tmp_path):
    from ke import hermes_api

    coord = make_coordinator(tmp_path, clock=clock, api=hermes_api)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    coord.evaluate(clock.now)
    assert coord.pending_dues == []
    assert coord.store.latest_wait("default", tid) is not None
    clock.advance(1800)
    coord.evaluate(clock.now)
    assert [(d.task_id, d.stage) for d in coord.pending_dues] == [(tid, "reminder1")]
    coord.evaluate(clock.now)  # nothing marks it sent yet, so it is offered again
    assert [d.stage for d in coord.pending_dues] == ["reminder1"]
    assert coord.store.latest_wait("default", tid)["stage"] is None


def test_conditions_wait_reads_tracker_rows(kanban_home, clock, tmp_path):
    from ke import conditions, hermes_api

    wt, store, settings = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    (due,) = wt.collect_due(clock.now)
    wt.mark_sent(due, clock.now)
    with hermes_api.connect("default") as conn:
        cc = conditions.explain(conn, "default", tid, store, clock.now + 600, settings)
    w = next(c for c in cc.conditions if c.name == "wait")
    assert w.ok is False and "sponsor" in w.text and "40 min" in w.text and "next reminder in 1 h 20 min" in w.text


# -- breaker parks are not human waits (final review I-1 / M-5) ------------------
def breaker_park(tid, board="default", error="AssertionError in tests"):
    """Unblock ``tid`` if needed, fail one run and trip Hermes' breaker; returns the ``gave_up`` event.

    ``block_kind`` deliberately survives the unblock in Hermes, so a card that was human-blocked
    before keeps ``needs_input`` on the breaker park.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kd
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        if hermes_api.task(conn, tid).status == "blocked":
            assert hermes_api.unblock(conn, tid)
        assert kb.claim_task(conn, tid) is not None
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome="crashed", status="crashed", error=error,
                        metadata={"exit_kind": "nonzero_exit"})
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (tid,),
            )
        assert kd._record_task_failure(
            conn, tid, error=error, outcome="crashed", failure_limit=1, release_claim=False, end_run=False,
        )
        assert hermes_api.task(conn, tid).status == "blocked"
        return max((e for e in hermes_api.events(conn, tid) if e.kind == "gave_up"), key=lambda e: e.id)


def _fleet_waits(store, settings, now):
    from ke import conditions, hermes_api

    return sorted(w["task_id"] for w in conditions.fleet_status(store, now, settings=settings, api=hermes_api)["human_waits"])


def test_a_breaker_park_is_not_a_human_wait(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, settings = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="crashy", assignee="dev")
    gu = breaker_park(tid)
    clock.now = float(gu.created_at)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is None
    assert _fleet_waits(store, settings, clock.now) == []


def test_block_version_counts_the_breaker_gave_up(kanban_home, clock, tmp_path):
    from ke import hermes_api
    from ke.waits import block_version

    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    gu = breaker_park(tid)
    with hermes_api.connect("default") as conn:
        assert hermes_api.task(conn, tid).block_kind == "needs_input"  # survived the unblock
        assert block_version(hermes_api.events(conn, tid)) == gu.id > ev.id


def test_a_breaker_repark_after_a_human_block_is_dropped_and_never_escalates(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, settings = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(1800)
    (due,) = wt.collect_due(clock.now)
    wt.mark_sent(due, clock.now)
    breaker_park(tid)
    from ke import conditions

    with hermes_api.connect("default") as conn:  # before the next sync the old row is not shown as a wait
        cc = conditions.explain(conn, "default", tid, store, clock.now, settings)
    assert next(c for c in cc.conditions if c.name == "wait").ok is True
    clock.now = float(ev.created_at) + 9 * H  # past the escalation of the OLD block
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is None
    assert wt.collect_due(clock.now) == []
    assert _fleet_waits(store, settings, clock.now) == []


def test_a_classified_breaker_park_is_a_new_wait_counted_from_its_own_time(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, settings = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="crashy", assignee="dev")
    breaker_park(tid)
    with hermes_api.connect("default") as conn:  # somebody classifies the park as a human wait
        assert block_task(conn, tid, reason="Preciso: a\nPor quê: b\nComo responder: c", kind="needs_input")
        ev = _last_blocked(conn, tid)
    clock.now = float(ev.created_at) + 60
    wt.sync(clock.now)
    row = store.latest_wait("default", tid)
    assert row["version"] == ev.id and row["started_at"] == ev.created_at
    assert wt.collect_due(clock.now) == []  # no immediate escalation
    clock.now = float(ev.created_at) + 1800
    assert _stages(wt.collect_due(clock.now)) == ["reminder1"]
    assert _fleet_waits(store, settings, clock.now) == [tid]


def test_fleet_status_and_the_tracker_share_one_definition_of_a_human_wait(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, settings = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        human, ev = make_human_block(conn, title="human")
        untyped, _ = make_human_block(conn, title="untyped", kind=None)
        transient, _ = make_human_block(conn, title="transient", kind="transient")
        stale, _ = make_human_block(conn, title="stale")
        crashy = create_task(conn, title="crashy", assignee="dev")
    breaker_park(stale)
    breaker_park(crashy)
    clock.now = float(ev.created_at) + 60
    wt.sync(clock.now)
    tracked = sorted(r["task_id"] for r in store.all_waits())
    assert tracked == sorted([human, untyped])
    assert _fleet_waits(store, settings, clock.now) == tracked


def test_current_block_of_a_breaker_park_carries_the_breaker_error(kanban_home, clock, tmp_path):
    from ke import hermes_api
    from ke.waits import current_block

    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="crashy", assignee="dev")
    gu = breaker_park(tid, error="Gateway connection failed")
    with hermes_api.connect("default") as conn:
        block = current_block(hermes_api.task(conn, tid), hermes_api.events(conn, tid))
    assert block.breaker and block.version == gu.id and block.started_at == gu.created_at
    assert block.reason == "Gateway connection failed" and block.kind is None


# -- block loop: Hermes routes a repeated block to triage ------------------------
def _loop_card(conn):
    """Block, unblock and re-block one card with the same kind: Hermes sends it to ``triage``."""
    from hermes_cli import kanban_db as kb

    tid, _ = make_human_block(conn, "Preciso: a\nPor quê: b\nComo responder: c")
    assert kb.unblock_task(conn, tid)
    assert kb.claim_task(conn, tid) is not None
    assert block_task(conn, tid, reason="Preciso: d\nPor quê: e\nComo responder: f", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_block_loop_in_triage_is_a_wait_with_reminders(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = _loop_card(conn)
        ev = max((e for e in hermes_api.events(conn, tid) if e.kind == "block_loop_detected"), key=lambda e: e.id)
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid)["version"] == ev.id
    clock.advance(1800)
    due = wt.collect_due(clock.now)
    assert _stages(due) == ["reminder1"] and "d" in due[0].ask.need


def test_plain_triage_card_is_not_a_wait(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, assignee="dev", triage=True)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid) is None


def test_answering_a_block_loop_puts_the_card_back_to_work(kanban_home, clock, tmp_path):
    from hermes_cli import kanban_db as kb
    from ke import hermes_api
    from ke.inbound import resolve_wait

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = _loop_card(conn)
    wt.sync(clock.now)
    version = store.latest_wait("default", tid)["version"]
    done, _held = resolve_wait(hermes_api, store, "default", tid, version, "bora", clock.now, "telegram:1")
    assert done
    with hermes_api.connect("default") as conn:
        assert kb.get_task(conn, tid).status == "ready"
