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


def test_human_kinds_setting_is_honored(kanban_home, clock, tmp_path):
    from ke import hermes_api

    wt, store, _ = _tracker(tmp_path, human_kinds=("needs_input", "capability"))
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, kind="capability")
    _start(clock, ev)
    wt.sync(clock.now)
    assert store.latest_wait("default", tid)["version"] == ev.id


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
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due("t_02690416", held=["t_cbdccb7a", "t_577bd508"])])
    lines = txt.splitlines()
    assert lines[0].startswith("🟠") and "voz" in lines[0] and "2 h" in lines[0]
    assert "ligue a placa" in txt and "o teste so roda com ela" in txt
    assert "2 cards" in txt and "t_cbdccb7a" in txt and "t_577bd508" in txt
    assert '"done"' in lines[-1] and lines[-1].endswith("hermes kanban-empowered done t_02690416")
    assert "1." not in txt


def test_render_omits_held_line_when_nothing_is_held():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due(held=[])])
    assert "t_1" in txt.splitlines()[-1]
    assert len(txt.splitlines()) == 4  # header, need, why, how to reply


def test_render_numbers_items_when_grouped():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due("t_a", board="b1"), _due("t_b", board="b2", held=["t_x"])])
    assert "1." in txt and "2." in txt and '"1 done"' in txt
    assert "t_a" in txt and "t_b" in txt and "t_x" in txt
    assert txt.splitlines()[-1].endswith("hermes kanban-empowered done <id>")


def test_render_raw_reason_without_ask():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [_due(ask=False, reason="aguardando o sponsor")])
    assert "aguardando o sponsor" in txt and "t_1" in txt


def test_render_marks_escalation_and_digest():
    from ke.messages import render_waits

    reminder = render_waits("sponsor", [_due(stage="reminder1")]).splitlines()[0]
    escalation = render_waits("sponsor", [_due(stage="escalation", age_s=8 * H)]).splitlines()[0]
    digest = render_waits("sponsor", [_due(stage="digest", age_s=32 * H)]).splitlines()[0]
    assert len({reminder, escalation, digest}) == 3
    assert "8 h" in escalation and "1 d 8 h" in digest
    grouped = render_waits("sponsor", [_due(stage="reminder1"), _due("t_2", stage="escalation")])
    assert grouped.splitlines()[0].startswith("🔴")  # the most severe stage sets the tone


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
    assert "Precisa de você" in single.splitlines()[0] and "parado há 2 h" in single.splitlines()[0]
    assert "O que preciso: ligue a placa" in single
    assert "Por quê: o teste so roda com ela" in single
    assert "O que está parado atrás: 2 cards (t_a, t_b)." in single
    assert single.splitlines()[-1] == (
        'Como responder: responda "feito" a esta mensagem ou rode: hermes kanban-empowered done t_02690416'
    )
    grouped = render_waits("sponsor", [_due("t_a"), _due("t_b")])
    assert "1." in grouped and "2." in grouped
    assert grouped.splitlines()[-1] == (
        'Como responder: responda "1 feito" a esta mensagem ou rode: hermes kanban-empowered done <id>'
    )


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
