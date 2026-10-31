import time

import pytest

from conftest import block_task, create_task, settings_for_test


# -- helpers -------------------------------------------------------------------
def _store(tmp_path):
    from ke.store import Store

    return Store(tmp_path / "state" / "state.db")


def _raw_event(conn, tid, kind, payload=None, run_id=None, ts=None):
    import json

    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (tid, run_id, kind, json.dumps(payload) if payload is not None else None, int(ts or time.time())),
    )
    conn.commit()


def _claim(conn, tid):
    from hermes_cli import kanban_db as kb

    task = kb.claim_task(conn, tid)
    assert task is not None
    return task


def _complete(conn, tid, summary="feito"):
    from hermes_cli import kanban_db as kb

    run_id = kb.get_task(conn, tid).current_run_id
    assert kb.complete_task(conn, tid, summary=summary, expected_run_id=run_id)
    return run_id


def _cond(cc, name):
    return next(c for c in cc.conditions if c.name == name)


def _explain(board, tid, store, now=None, **kw):
    from ke import conditions, hermes_api as h

    with h.connect(board) as conn:
        return conditions.explain(conn, board, tid, store, now if now is not None else time.time() + 5, **kw)


# -- human_age -----------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0 s"),
        (12, "12 s"),
        (59, "59 s"),
        (60, "1 min"),
        (40 * 60, "40 min"),
        (3600, "1 h"),
        (5 * 3600 + 20 * 60, "5 h 20 min"),
        (2 * 86400 + 3 * 3600, "2 d 3 h"),
        (2 * 86400, "2 d"),
        (-5, "0 s"),
    ],
)
def test_human_age(seconds, expected):
    from ke.messages import human_age

    assert human_age(seconds) == expected


# -- dependencies ---------------------------------------------------------------
def test_dependencies_walks_to_blocked_root(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        raiz = create_task(conn, title="raiz", assignee="dev")
        _claim(conn, raiz)
        assert block_task(conn, raiz, reason="Preciso: x", kind="needs_input")
        filho = create_task(conn, title="filho", parents=[raiz])
        neto = create_task(conn, title="neto", parents=[filho])
    cc = _explain("default", neto, store)
    dep = _cond(cc, "dependencies")
    assert dep.ok is False and raiz in dep.text and "needs_input" in dep.text
    assert filho not in dep.text  # reports the root, not the intermediate
    assert dep.text.startswith("waiting on ")
    assert "blocked needs_input" in dep.text and "owner:" in dep.text


def test_dependencies_ok_without_open_parents(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="solo")
    dep = _cond(_explain("default", tid, store), "dependencies")
    assert dep.ok is True


def test_dependencies_ignores_done_parents(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        pai = create_task(conn, title="pai", assignee="dev")
        _claim(conn, pai)
        _complete(conn, pai)
        filho = create_task(conn, title="filho", parents=[pai])
    assert _cond(_explain("default", filho, store), "dependencies").ok is True


def test_dependencies_owner_of_human_block_is_board_owner(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    settings = settings_for_test(tmp_path, default_owner="dona-maria")
    with h.connect("default") as conn:
        raiz = create_task(conn, title="raiz", assignee="dev")
        _claim(conn, raiz)
        block_task(conn, raiz, reason="Preciso: x", kind="needs_input")
        filho = create_task(conn, title="filho", parents=[raiz])
    dep = _cond(_explain("default", filho, store, settings=settings), "dependencies")
    assert "owner: dona-maria" in dep.text


# -- fresh ----------------------------------------------------------------------
def _completed_with_summary_then_comments(n_comments=2):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _claim(conn, tid)
        _complete(conn, tid, summary="entregue")
        # make the summary clearly older than what follows (Hermes stamps whole seconds)
        conn.execute("UPDATE task_runs SET started_at = started_at - 100, ended_at = ended_at - 100")
        conn.commit()
        for i in range(n_comments):
            h.comment(conn, tid, f"nota {i}")
    return tid


def test_fresh_flags_comments_after_summary(kanban_home, tmp_path):
    store = _store(tmp_path)
    tid = _completed_with_summary_then_comments(2)
    fresh = _cond(_explain("default", tid, store), "fresh")
    assert fresh.ok is False and "2 comments" in fresh.text


def test_fresh_singular_comment(kanban_home, tmp_path):
    store = _store(tmp_path)
    tid = _completed_with_summary_then_comments(1)
    assert "1 comment" in _cond(_explain("default", tid, store), "fresh").text


def test_fresh_ok_when_nothing_after_summary(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _claim(conn, tid)
        _complete(conn, tid, summary="entregue")
    fresh = _cond(_explain("default", tid, store, now=time.time() + 5), "fresh")
    assert fresh.ok is True


def test_fresh_ignores_heartbeat_events(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _claim(conn, tid)
        _complete(conn, tid, summary="entregue")
        for _ in range(3):
            _raw_event(conn, tid, "heartbeat", {"note": "x"})
    fresh = _cond(_explain("default", tid, store), "fresh")
    assert fresh.ok is True


def test_fresh_counts_non_heartbeat_events_after_summary(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _claim(conn, tid)
        _complete(conn, tid, summary="entregue")
        _raw_event(conn, tid, "reprioritized", {"priority": 3})
        _raw_event(conn, tid, "heartbeat")
    fresh = _cond(_explain("default", tid, store), "fresh")
    assert fresh.ok is False and "1 event" in fresh.text and "heartbeat" not in fresh.text


def test_fresh_without_summary_is_ok(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
    fresh = _cond(_explain("default", tid, store), "fresh")
    assert fresh.ok is True and "no summary" in fresh.text


# -- guard ----------------------------------------------------------------------
def test_guard_reports_reason_count_and_since(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    now = time.time()
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _raw_event(conn, tid, "respawn_guarded", {"reason": "falhas consecutivas"}, ts=now - 3600)
        _raw_event(conn, tid, "respawn_guarded", {"reason": "perfil sem credencial"}, ts=now - 600)
    g = _cond(_explain("default", tid, store, now=now), "guard")
    assert g.ok is False
    assert "perfil sem credencial" in g.text and "falhas consecutivas" not in g.text
    assert "2x" in g.text and "1 h" in g.text


def test_guard_resets_after_new_claim(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _raw_event(conn, tid, "respawn_guarded", {"reason": "x"})
        _claim(conn, tid)
    assert _cond(_explain("default", tid, store), "guard").ok is True


def test_guard_ignores_heartbeat(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _raw_event(conn, tid, "heartbeat")
    assert _cond(_explain("default", tid, store), "guard").ok is True


# -- live -----------------------------------------------------------------------
def test_live_running_reports_run_pid_and_heartbeat(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    now = time.time()
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        task = _claim(conn, tid)
        conn.execute(
            "UPDATE tasks SET worker_pid = 4242, last_heartbeat_at = ? WHERE id = ?",
            (int(now - 120), tid),
        )
        conn.commit()
    live = _cond(_explain("default", tid, store, now=now), "live")
    assert live.ok is True
    assert f"run #{task.current_run_id}" in live.text and "4242" in live.text and "2 min" in live.text


def test_live_not_running(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
    live = _cond(_explain("default", tid, store), "live")
    assert live.ok is True and "no active run" in live.text


# -- wait -----------------------------------------------------------------------
def test_wait_without_row_is_graceful(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
    w = _cond(_explain("default", tid, store), "wait")
    assert w.ok is True and "no wait recorded" in w.text


def test_wait_with_row_shows_owner_age_and_next_reminder(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    now = time.time()
    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="dev")
        _claim(conn, tid)
        block_task(conn, tid, reason="Preciso: x", kind="needs_input")
    store.conn.execute(
        "INSERT INTO waits (board, task_id, version, owner, started_at, stage, last_sent_at) "
        "VALUES ('default', ?, 5, 'sponsor', ?, NULL, NULL)",
        (tid, int(now - 600)),
    )
    w = _cond(_explain("default", tid, store, now=now), "wait")
    assert w.ok is False
    assert "sponsor" in w.text and "10 min" in w.text and "next reminder in 20 min" in w.text


# -- order / formatting ---------------------------------------------------------
def test_condition_order(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
    cc = _explain("default", tid, store)
    assert [c.name for c in cc.conditions] == ["dependencies", "guard", "live", "fresh", "wait"]
    assert cc.board == "default" and cc.task_id == tid and cc.status == "ready"


def test_explain_unknown_task_raises(kanban_home, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(LookupError):
        _explain("default", "t_nope", store)


def test_explain_output_has_one_line_per_condition(kanban_home, tmp_path):
    from ke import conditions

    store = _store(tmp_path)
    tid = _completed_with_summary_then_comments(2)
    out = conditions.format_explain(_explain("default", tid, store))
    assert out.count("\n") >= 5
    lines = out.splitlines()
    assert lines[0].startswith(tid) and "  t  " in lines[0] and " ago" in lines[0]
    assert any("✗" in l or "⚠" in l for l in lines[1:]) and any("✓" in l for l in lines[1:])


# -- fleet_status ---------------------------------------------------------------
def test_fleet_status_keys_and_counts(kanban_home, tmp_path):
    from hermes_cli import kanban_db_boards as kbb
    from ke import conditions, hermes_api as h

    store = _store(tmp_path)
    kbb.create_board("side")
    now = time.time()
    with h.connect("default") as conn:
        run_t = create_task(conn, title="rodando", assignee="dev")
        _claim(conn, run_t)
        wait_t = create_task(conn, title="esperando", assignee="dev")
        _claim(conn, wait_t)
        block_task(conn, wait_t, reason="Preciso: x", kind="needs_input")
        guard_t = create_task(conn, title="travado", assignee="dev")
        _raw_event(conn, guard_t, "respawn_guarded", {"reason": "r"})
    with h.connect("side") as conn:
        create_task(conn, title="outro")
    store.log_action("default", run_t, "human_done", None, int(now) - 60)
    store.log_action("default", run_t, "guard_release", None, int(now) - 30)
    store.log_action("default", run_t, "something_else", None, int(now) - 30)

    fs = conditions.fleet_status(store, now + 1)
    assert set(fs) == {"running", "human_waits", "guarded", "resolved_today", "boards"}
    assert [r["task_id"] for r in fs["running"]] == [run_t]
    assert [w["task_id"] for w in fs["human_waits"]] == [wait_t]
    assert [g["task_id"] for g in fs["guarded"]] == [guard_t]
    assert fs["guarded"][0]["reason"] == "r"
    assert fs["resolved_today"] == 2
    assert fs["boards"] == ["default", "side"]


# -- CLI ------------------------------------------------------------------------
def test_cli_explain_finds_card_across_boards(kanban_home, tmp_path, capsys):
    from hermes_cli import kanban_db_boards as kbb
    from ke import cli, hermes_api as h

    kbb.create_board("side")
    with h.connect("side") as conn:
        tid = create_task(conn, title="no-side")
    rc = cli.run_explain(settings_for_test(tmp_path), tid, None)
    out = capsys.readouterr().out
    assert rc == 0 and tid in out and "no-side" in out and "dependencies" in out


def test_cli_explain_unknown_card(kanban_home, tmp_path, capsys):
    from ke import cli

    rc = cli.run_explain(settings_for_test(tmp_path), "t_nope", None)
    assert rc == 1 and "not found" in capsys.readouterr().out


def test_cli_status_prints_summary(kanban_home, tmp_path, capsys):
    from ke import cli

    rc = cli.run_status(settings_for_test(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0 and "running" in out and "resolved" in out


def test_cli_setup_registers_subcommands():
    import argparse

    from ke import cli

    p = argparse.ArgumentParser()
    cli.setup(p)
    a = p.parse_args(["explain", "t_1", "--board", "matrix"])
    assert (a.ke_cmd, a.card, a.board) == ("explain", "t_1", "matrix")
    assert p.parse_args(["status"]).ke_cmd == "status"


# -- i18n -----------------------------------------------------------------------
def test_catalog_key_parity_and_placeholders():
    import string

    from agent.i18n_layers import parse_locale_file
    from ke import messages

    root = __import__("pathlib").Path(__file__).resolve().parent.parent / "locales"
    en_yaml = parse_locale_file(root / "en.yaml")
    pt_yaml = parse_locale_file(root / "pt-br.yaml")
    en_code = {messages.PREFIX + k: v for k, v in messages.EN.items()}
    assert set(en_code) == set(en_yaml) == set(pt_yaml)
    assert en_code == en_yaml  # the shipped English catalog is exactly the built-in one

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for key, text in en_yaml.items():
        assert fields(pt_yaml[key]) == fields(text), key


def test_plugin_yaml_declares_locales():
    import pathlib

    import yaml

    data = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8"))
    ids = [(e["id"] if isinstance(e, dict) else e) for e in data["provides_locales"]]
    assert ids == ["en", "pt-br"]


def test_t_falls_back_to_builtin_english_when_hermes_has_no_translation(monkeypatch):
    from ke import hermes_api, messages

    monkeypatch.setattr(hermes_api, "translate", lambda key, **kw: None)
    assert messages.t("explain.wait.none") == "no wait recorded"
    assert messages.t("age.min", n=3) == "3 min"
    assert messages.t("no.such.key") == "no.such.key"


def test_translate_returns_none_for_unknown_key(kanban_home):
    from ke import hermes_api

    assert hermes_api.translate("kanban_empowered.definitely.missing") is None


@pytest.fixture
def pt_br(kanban_home, monkeypatch):
    """Register the shipped pt-br pack in Hermes and select the language."""
    import pathlib

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


def test_output_follows_hermes_language(pt_br, tmp_path):
    from ke import conditions, hermes_api as h
    from ke.messages import human_age

    store = _store(tmp_path)
    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
    out = conditions.format_explain(_explain("default", tid, store))
    assert " há " in out.splitlines()[0]
    assert "sem espera registrada" in out and "sem dependências abertas" in out
    assert human_age(5 * 3600 + 20 * 60) == "5 h 20 min"


def test_english_when_pack_missing_for_language(kanban_home, tmp_path, monkeypatch):
    from agent import i18n

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        from ke import conditions, hermes_api as h

        store = _store(tmp_path)
        with h.connect("default") as conn:
            tid = create_task(conn, title="t")
        assert "no wait recorded" in conditions.format_explain(_explain("default", tid, store))
    finally:
        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        i18n.reset_language_cache()


# -- dependency graph shapes ----------------------------------------------------
def test_dependencies_diamond_reports_only_the_root(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        r = create_task(conn, title="raiz", assignee="dev")
        _claim(conn, r)
        block_task(conn, r, reason="Preciso: x", kind="needs_input")
        p1 = create_task(conn, title="p1", parents=[r])
        p2 = create_task(conn, title="p2", parents=[r])
        x = create_task(conn, title="x", parents=[p1, p2])
    dep = _cond(_explain("default", x, store), "dependencies")
    assert dep.text.count(r) == 1 and p1 not in dep.text and p2 not in dep.text


def test_dependencies_two_independent_roots(kanban_home, tmp_path):
    from ke import hermes_api as h

    store = _store(tmp_path)
    with h.connect("default") as conn:
        roots = []
        for name in ("a", "b"):
            r = create_task(conn, title=name, assignee="dev")
            _claim(conn, r)
            block_task(conn, r, reason="Preciso: x", kind="needs_input")
            roots.append(r)
        x = create_task(conn, title="x", parents=roots)
    dep = _cond(_explain("default", x, store), "dependencies")
    assert all(r in dep.text for r in roots)


def _fake_graph(monkeypatch, edges, statuses):
    from types import SimpleNamespace

    from ke import hermes_api as h

    monkeypatch.setattr(h, "parents", lambda conn, tid: edges.get(tid, []))
    monkeypatch.setattr(
        h,
        "task",
        lambda conn, tid: SimpleNamespace(id=tid, status=statuses.get(tid, "todo")),
    )


def test_open_roots_survives_cycle(monkeypatch):
    from ke import conditions

    _fake_graph(monkeypatch, {"a": ["b"], "b": ["c"], "c": ["b"]}, {})
    roots = conditions._open_roots(None, "a")
    assert [r.id for r in roots] == ["b"]  # cycle b<->c has no root: falls back to direct parents


def test_open_roots_cycle_with_real_root(monkeypatch):
    from ke import conditions

    _fake_graph(monkeypatch, {"a": ["b"], "b": ["c", "r"], "c": ["b"]}, {"r": "blocked"})
    assert [r.id for r in conditions._open_roots(None, "a")] == ["r"]


def test_open_roots_diamond_dedupes(monkeypatch):
    from ke import conditions

    _fake_graph(monkeypatch, {"x": ["p1", "p2"], "p1": ["r"], "p2": ["r"]}, {})
    assert [r.id for r in conditions._open_roots(None, "x")] == ["r"]


# -- fleet_status resilience ----------------------------------------------------
def test_fleet_status_skips_unreadable_board(kanban_home, tmp_path, monkeypatch):
    from hermes_cli import kanban_db_boards as kbb
    from ke import conditions, hermes_api as h

    store = _store(tmp_path)
    kbb.create_board("side")
    with h.connect("default") as conn:
        run_t = create_task(conn, title="rodando", assignee="dev")
        _claim(conn, run_t)
    real = h.connect

    def flaky(board):
        if board == "side":
            raise RuntimeError("corrompido")
        return real(board)

    monkeypatch.setattr(h, "connect", flaky)
    fs = conditions.fleet_status(store, time.time() + 1)
    assert [r["task_id"] for r in fs["running"]] == [run_t]
    assert fs["boards"] == ["default", "side"]


# -- CLI error paths ------------------------------------------------------------
def test_cli_explain_ambiguous_across_boards(kanban_home, tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import kanban_db_boards as kbb
    from ke import cli, conditions, hermes_api as h

    kbb.create_board("side")
    monkeypatch.setattr(h, "task", lambda conn, tid: SimpleNamespace(id=tid))
    monkeypatch.setattr(
        conditions,
        "explain",
        lambda conn, board, tid, store, now, settings=None: conditions.CardConditions(
            board, tid, "ready", 0, [], "x"
        ),
    )
    rc = cli.run_explain(settings_for_test(tmp_path), "t_dup", None)
    out = capsys.readouterr().out
    assert rc == 1 and "several boards" in out and "default, side" in out and "--board" in out


def test_cli_explain_board_error_exits_1(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, hermes_api as h

    def boom(board):
        raise RuntimeError("sem acesso")

    monkeypatch.setattr(h, "connect", boom)
    rc = cli.run_explain(settings_for_test(tmp_path), "t_1", "matrix")
    out = capsys.readouterr().out
    assert rc == 1 and "error reading board matrix" in out and "sem acesso" in out


def test_cli_explain_failure_is_not_reported_as_not_found(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, conditions, hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t")

    def boom(*a, **kw):
        raise RuntimeError("falha interna")

    monkeypatch.setattr(conditions, "explain", boom)
    rc = cli.run_explain(settings_for_test(tmp_path), tid, None)
    out = capsys.readouterr().out
    assert rc == 1 and "error reading board default" in out and "falha interna" in out
    assert "not found" not in out
