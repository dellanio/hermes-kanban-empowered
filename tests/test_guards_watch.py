"""Task 10: GuardWatch (alert after 30 min of respawn_guarded) and the honest ``release`` command."""
from __future__ import annotations

import time
import types

import pytest

from conftest import block_task, create_task, make_coordinator, settings_for_test
from test_conditions import _claim, _raw_event
from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)

OWNER_CHAT = 6802889854
PR = "https://github.com/dellanio/kanban-x/pull/17"


# -- helpers -------------------------------------------------------------------
def _store(tmp_path):
    from ke.store import Store

    return Store(tmp_path / "state" / "state.db")


def _gw(tmp_path, api=None, **overrides):
    from ke.guards_watch import GuardWatch

    settings = settings_for_test(tmp_path, **overrides)
    store = _store(tmp_path)
    return GuardWatch(settings, store, api), store


@pytest.fixture
def api():
    from ke import hermes_api

    return hermes_api


class ApiWithProfiles:
    """``hermes_api`` with a fixed profile list (the real one reads the Hermes home directory)."""

    def __init__(self, profiles=("default", "dev", "reviewer", "devops")):
        from ke import hermes_api

        self._api = hermes_api
        self._profiles = list(profiles)
        self.assign_error = None

    def profiles(self):
        return list(self._profiles)

    def veto_hooks_available(self):  # the unpatched Hermes under test would add a setup warning
        return True

    def assign(self, conn, tid, profile):
        if self.assign_error is not None:
            raise self.assign_error
        return self._api.assign(conn, tid, profile)

    def __getattr__(self, name):
        return getattr(self._api, name)


def _guarded_card(conn, reason="active_pr", assignee="dev", title="card", ts=None):
    tid = create_task(conn, title=title, assignee=assignee)
    _raw_event(conn, tid, "respawn_guarded", {"reason": reason}, ts=ts)
    return tid


def _assignee(board, tid):
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        return hermes_api.task(conn, tid).assignee


# -- observe / due_alerts --------------------------------------------------------
def test_alert_only_after_30_min(tmp_path, clock):
    gw, store = _gw(tmp_path)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    clock.advance(1799)
    assert gw.due_alerts(clock.now) == []
    clock.advance(1)
    assert [a["task_id"] for a in gw.due_alerts(clock.now)] == ["t1"]


def test_due_alert_carries_board_reason_and_age(tmp_path, clock):
    gw, _ = _gw(tmp_path)
    gw.observe("tw", [("t1", "blocker_auth")], clock.now)
    clock.advance(2000)
    (a,) = gw.due_alerts(clock.now)
    assert (a["board"], a["task_id"], a["reason"], a["age_s"]) == ("tw", "t1", "blocker_auth", 2000)


def test_alert_after_setting_is_honored(tmp_path, clock):
    gw, _ = _gw(tmp_path, guard_alert_after_s=60)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    clock.advance(60)
    assert len(gw.due_alerts(clock.now)) == 1


def test_new_reason_resets_the_clock_and_the_alert(tmp_path, clock):
    gw, store = _gw(tmp_path)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    clock.advance(1800)
    (a,) = gw.due_alerts(clock.now)
    gw.mark_alerted(a, clock.now)
    assert gw.due_alerts(clock.now) == []
    clock.advance(10)
    gw.observe("tw", [("t1", "blocker_auth")], clock.now)  # a different guard: a new episode
    assert gw.due_alerts(clock.now) == []
    row = store.get_guard_alert("tw", "t1")
    assert row["reason"] == "blocker_auth" and row["first_seen"] == int(clock.now) and row["alerted_at"] is None
    clock.advance(1800)
    assert [x["task_id"] for x in gw.due_alerts(clock.now)] == ["t1"]


def test_same_reason_keeps_first_seen_and_alerted_state(tmp_path, clock):
    gw, store = _gw(tmp_path)
    t0 = int(clock.now)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    clock.advance(1800)
    (a,) = gw.due_alerts(clock.now)
    gw.mark_alerted(a, clock.now)
    clock.advance(60)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    row = store.get_guard_alert("tw", "t1")
    assert row["first_seen"] == t0 and row["last_seen"] == int(clock.now) and row["alerted_at"] == t0 + 1800
    clock.advance(86400)
    assert gw.due_alerts(clock.now) == []  # one alert per guard episode


def test_mark_alerted_ignores_a_changed_reason(tmp_path, clock):
    gw, store = _gw(tmp_path)
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    clock.advance(1800)
    (stale,) = gw.due_alerts(clock.now)
    gw.observe("tw", [("t1", "blocker_auth")], clock.now)  # changed while the message was in flight
    gw.mark_alerted(stale, clock.now)
    assert store.get_guard_alert("tw", "t1")["alerted_at"] is None


def test_card_absent_three_cycles_is_removed(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    with api.connect("default") as conn:
        tid = _guarded_card(conn)
    gw.observe("default", [(tid, "active_pr")], clock.now)
    for _ in range(2):
        clock.advance(30)
        gw.observe("default", [], clock.now)
        assert store.get_guard_alert("default", tid) is not None  # still ready: could be a capped tick
    clock.advance(30)
    gw.observe("default", [], clock.now)
    assert store.get_guard_alert("default", tid) is None


def test_reappearing_card_resets_the_absence_count(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    with api.connect("default") as conn:
        tid = _guarded_card(conn)
    gw.observe("default", [(tid, "active_pr")], clock.now)
    gw.observe("default", [], clock.now + 1)
    gw.observe("default", [], clock.now + 2)
    gw.observe("default", [(tid, "active_pr")], clock.now + 3)
    gw.observe("default", [], clock.now + 4)
    gw.observe("default", [], clock.now + 5)
    assert store.get_guard_alert("default", tid) is not None  # 2 consecutive, not 4


def test_absences_are_counted_per_board(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    with api.connect("default") as conn:
        tid = _guarded_card(conn)
    gw.observe("default", [(tid, "active_pr")], clock.now)
    for _ in range(5):  # other boards ticking says nothing about "default"
        gw.observe("other", [], clock.now)
    assert store.get_guard_alert("default", tid) is not None


def test_card_that_left_ready_is_removed_at_once(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    with api.connect("default") as conn:
        tid = _guarded_card(conn)
    gw.observe("default", [(tid, "active_pr")], clock.now)
    with api.connect("default") as conn:
        _claim(conn, tid)  # running now
    gw.observe("default", [], clock.now + 30)
    assert store.get_guard_alert("default", tid) is None


def test_vanished_card_is_removed_at_once(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    gw.observe("default", [("t_gone", "active_pr")], clock.now)
    gw.observe("default", [], clock.now + 30)
    assert store.get_guard_alert("default", "t_gone") is None


def test_unreadable_board_only_counts_the_absence(tmp_path, clock):
    class Broken:
        def connect(self, board):
            raise OSError("db locked")

    gw, store = _gw(tmp_path, api=Broken())
    gw.observe("tw", [("t1", "active_pr")], clock.now)
    gw.observe("tw", [], clock.now + 1)
    assert store.get_guard_alert("tw", "t1") is not None
    gw.observe("tw", [], clock.now + 2)
    gw.observe("tw", [], clock.now + 3)
    assert store.get_guard_alert("tw", "t1") is None


# -- enrich: card details and the PR ------------------------------------------------
def test_enrich_adds_title_assignee_and_newest_pr(kanban_home, tmp_path, clock, api):
    gw, store = _gw(tmp_path, api=api)
    now = int(time.time())
    with api.connect("default") as conn:
        tid = _guarded_card(conn, title="ship it", ts=now - 4000)
        api.comment(conn, tid, "old pr https://github.com/o/r/pull/1 here")
        api.comment(conn, tid, "no link in this one")
        api.comment(conn, tid, f"opened {PR}.")
    gw.observe("default", [(tid, "active_pr")], now - 4000)
    (a,) = gw.enrich(gw.due_alerts(now), now)
    assert (a["title"], a["assignee"]) == ("ship it", "dev")
    assert a["pr_url"] == PR and isinstance(a["pr_at"], int)


def test_enrich_other_reasons_have_no_pr(kanban_home, tmp_path, api):
    gw, _ = _gw(tmp_path, api=api)
    now = int(time.time())
    with api.connect("default") as conn:
        tid = _guarded_card(conn, reason="blocker_auth")
        api.comment(conn, tid, f"see {PR}")
    gw.observe("default", [(tid, "blocker_auth")], now - 4000)
    (a,) = gw.enrich(gw.due_alerts(now), now)
    assert a.get("pr_url") is None


def test_enrich_drops_a_card_that_is_no_longer_waiting(kanban_home, tmp_path, api):
    gw, store = _gw(tmp_path, api=api)
    now = int(time.time())
    with api.connect("default") as conn:
        tid = _guarded_card(conn)
        _claim(conn, tid)
    gw.observe("default", [(tid, "active_pr")], now - 4000)
    assert gw.enrich(gw.due_alerts(now), now) == []
    assert store.get_guard_alert("default", tid) is None


# -- coordinator: feeding and the Telegram alert -----------------------------------
def _result(*pairs):
    return types.SimpleNamespace(respawn_guarded=list(pairs))


def _coord(tmp_path, clock, fake_tg, api, **kw):
    kw.setdefault("owner_ids", {"sponsor": OWNER_CHAT})
    return make_coordinator(tmp_path, clock=clock, telegram=fake_tg, api=ApiWithProfiles(), **kw)


def test_every_tick_feeds_observe_even_when_evaluate_is_throttled(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api)
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    with api.connect("default") as conn:
        b = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(5)  # throttled: no evaluation, but the observation of this board still lands
    c.on_tick("other", _result((b, "blocker_auth")))
    assert c.store.get_guard_alert("default", a)["first_seen"] == int(clock.now) - 5
    assert c.store.get_guard_alert("other", b)["reason"] == "blocker_auth"


def test_dry_run_and_missing_result_do_not_observe(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api)
    c.on_tick("default", _result(("t1", "active_pr")), dry_run=True)
    c.on_tick("default", None)
    assert c.store.get_guard_alert("default", "t1") is None


def test_non_leader_does_not_observe(kanban_home, tmp_path, clock, fake_tg, api):
    c = make_coordinator(tmp_path, coordinator_profile="default", active_profile="builder", clock=clock)
    c.on_tick("default", _result(("t1", "active_pr")))
    assert c.store.get_guard_alert("default", "t1") is None


def test_observe_failure_does_not_stop_the_evaluation(kanban_home, tmp_path, clock, fake_tg, api, caplog):
    c = _coord(tmp_path, clock, fake_tg, api)

    def boom(*a, **k):
        raise RuntimeError("db")

    c.guards.observe = boom
    c.on_tick("default", _result(("t1", "active_pr")))
    assert c.store.kv_get("last_eval") is not None


def test_one_message_with_all_due_alerts_and_the_release_instruction(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api)
    now = int(clock.now)
    with api.connect("default") as conn:
        a = _guarded_card(conn, title="alpha", ts=now)
        api.comment(conn, a, f"PR is {PR}")
        b = _guarded_card(conn, reason="blocker_auth", assignee="reviewer", title="beta", ts=now)
    c.on_tick("default", _result((a, "active_pr"), (b, "blocker_auth")))
    assert fake_tg.count("send") == 0  # nothing before 30 min
    clock.advance(1769)  # evaluations run at most every 30 s: this one is 1769 s in, still below 30 min
    c.on_tick("default", _result((a, "active_pr"), (b, "blocker_auth")))
    assert fake_tg.count("send") == 0
    clock.advance(31)
    c.on_tick("default", _result((a, "active_pr"), (b, "blocker_auth")))
    assert fake_tg.count("send") == 1
    call = fake_tg.calls["send"][0]
    text = call["text"]
    assert call["chat_id"] == OWNER_CHAT
    assert a in text and b in text and "alpha" in text and "beta" in text
    assert "hermes kanban-empowered release <id> --to <profile>" in text
    assert PR in text
    sent = c.store.get_sent_message(OWNER_CHAT, call["message_id"])
    assert sent["kind"] == "guard"
    assert {(i["board"], i["task_id"]) for i in sent["items"]} == {("default", a), ("default", b)}
    assert c.store.get_guard_alert("default", a)["alerted_at"] is not None
    # alerted once per episode
    clock.advance(3600)
    c.on_tick("default", _result((a, "active_pr"), (b, "blocker_auth")))
    assert fake_tg.count("send") == 1


def test_alert_text_names_the_pr_and_when(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api)
    now = int(clock.now)
    with api.connect("default") as conn:
        a = _guarded_card(conn, ts=now)
        api.comment(conn, a, f"PR is {PR}")
        cm = api.comments(conn, a)[-1]
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    c.on_tick("default", _result((a, "active_pr")))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(cm.created_at))
    assert f"Latest PR: {PR} (commented {when})" in fake_tg.last_text


def test_alerts_go_to_the_owner_of_each_board(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(
        tmp_path, clock, fake_tg, api,
        owner_ids={"sponsor": OWNER_CHAT, "ana": 777}, board_owner={"default": "ana"},
    )
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    c.on_tick("default", _result((a, "active_pr")))
    assert [x["chat_id"] for x in fake_tg.calls["send"]] == [777]


def test_failed_send_is_retried_and_not_marked(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api)
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    fake_tg.fail_next = OSError("network down")
    c.on_tick("default", _result((a, "active_pr")))
    assert c.store.get_guard_alert("default", a)["alerted_at"] is None
    clock.advance(30)
    c.on_tick("default", _result((a, "active_pr")))
    assert fake_tg.count("send") == 1 and c.store.get_guard_alert("default", a)["alerted_at"] is not None


def test_missing_chat_id_keeps_the_alert_due(kanban_home, tmp_path, clock, fake_tg, api):
    c = _coord(tmp_path, clock, fake_tg, api, owner_ids={})
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    c.on_tick("default", _result((a, "active_pr")))
    assert fake_tg.count("send") == 0
    assert c.store.get_guard_alert("default", a)["alerted_at"] is None


def test_without_telegram_nothing_breaks(kanban_home, tmp_path, clock, api):
    c = make_coordinator(tmp_path, clock=clock, api=ApiWithProfiles(), owner_ids={"sponsor": OWNER_CHAT})
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    c.on_tick("default", _result((a, "active_pr")))
    assert c.store.get_guard_alert("default", a)["alerted_at"] is None


def test_guard_message_is_never_a_wait_reply_target(kanban_home, tmp_path, clock, fake_tg, api):
    """Task 9's inbound resolves only kind "waits": a "done" to a guard alert changes nothing."""
    from test_inbound import _event

    from ke.inbound import handle_inbound

    c = _coord(tmp_path, clock, fake_tg, api)
    with api.connect("default") as conn:
        a = _guarded_card(conn)
    c.on_tick("default", _result((a, "active_pr")))
    clock.advance(1800)
    c.on_tick("default", _result((a, "active_pr")))
    mid = fake_tg.calls["send"][0]["message_id"]
    ev = _event("done", OWNER_CHAT, chat_id=OWNER_CHAT, reply_to=mid)
    assert handle_inbound(ev, c.settings, c.store, api, fake_tg, clock.now) is None


def test_messages_are_split_to_the_telegram_limit(tmp_path):
    from ke.messages import TELEGRAM_LIMIT, _tg_len, render_guard_chunks

    alerts = [
        {"board": "b", "task_id": f"t_{i:08x}", "reason": "active_pr", "age_s": 2000,
         "title": "x" * 300, "assignee": "dev", "pr_url": PR + "0" * 200, "pr_at": 1_700_000_000}
        for i in range(40)
    ]
    chunks = render_guard_chunks(alerts)
    assert len(chunks) > 1
    assert all(_tg_len(text) <= TELEGRAM_LIMIT for text, _ in chunks)
    assert [a for _, items in chunks for a in items] == alerts


# -- release ---------------------------------------------------------------------
def _release(tid, to, board="default", api=None, **kw):
    from ke import hermes_api
    from ke.guards_watch import release

    api = api or ApiWithProfiles()
    with hermes_api.connect(board) as conn:
        return release(conn, board, tid, to, api, **kw)


def test_release_hands_the_card_to_another_profile(kanban_home, tmp_path):
    from ke import hermes_api

    store = _store(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    out = _release(tid, "reviewer", store=store, now=1_700_000_000, actor="cli:ana")
    assert out == "released: assigned to reviewer"
    assert _assignee("default", tid) == "reviewer"
    with hermes_api.connect("default") as conn:
        body = hermes_api.comments(conn, tid)[-1].body
    assert body.startswith("kanban-empowered:") and "active_pr" in body and "reviewer" in body and "dev" in body
    (act,) = [a for a in store.actions_since(0) if a["kind"] == "guard_release"]
    assert (act["board"], act["task_id"]) == ("default", tid) and "reviewer" in act["detail"]


def test_release_also_works_for_blocker_auth(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn, reason="blocker_auth")
    assert _release(tid, "devops").startswith("released")
    assert _assignee("default", tid) == "devops"


def test_release_refuses_unknown_profile(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    out = _release(tid, "piloto-inexistente")
    assert out == "profile piloto-inexistente does not exist"
    assert _assignee("default", tid) == "dev"


def test_release_same_profile_is_not_a_handoff(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    out = _release(tid, "dev")
    assert "same profile" in out
    assert _assignee("default", tid) == "dev"
    with hermes_api.connect("default") as conn:
        assert not [c for c in hermes_api.comments(conn, tid)]


def test_release_without_guard_reports_truth(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="calm", assignee="dev")
    out = _release(tid, "devops")
    assert out == "no compatible exit: no guard"
    assert _assignee("default", tid) == "dev"


def test_release_with_another_reason_names_it(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn, reason="rate_limit_cooldown")
    out = _release(tid, "devops")
    assert out == "no compatible exit: rate_limit_cooldown"
    assert _assignee("default", tid) == "dev"


def test_release_ignores_a_guard_from_before_the_last_claim(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
        _claim(conn, tid)
    out = _release(tid, "devops")
    assert out.startswith("no compatible exit")


def test_release_uses_the_latest_guard_reason(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn, reason="rate_limit_cooldown", ts=int(time.time()) - 100)
        _raw_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
    assert _release(tid, "devops").startswith("released")


def test_release_of_a_running_card_is_reported(kanban_home):
    from ke import hermes_api

    api = ApiWithProfiles()
    api.assign_error = RuntimeError("cannot reassign: currently running")
    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    out = _release(tid, "devops", api=api)
    assert out == "card is running; release not possible now"
    assert _assignee("default", tid) == "dev"
    with hermes_api.connect("default") as conn:
        assert not hermes_api.comments(conn, tid)


def test_release_of_a_card_not_waiting_to_run_is_refused(kanban_home):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
        _claim(conn, tid)
    assert _release(tid, "devops").startswith("no compatible exit")


def test_release_in_pt_br(pt_br):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        ok = _guarded_card(conn)
        other = _guarded_card(conn)
        calm = create_task(conn, title="calm", assignee="dev")
    assert _release(ok, "reviewer") == "liberado: atribuído a reviewer"
    assert _release(other, "piloto-inexistente") == "perfil piloto-inexistente não existe"
    assert _release(other, "dev") == "handoff para o mesmo perfil não libera a trava"
    assert _release(calm, "devops") == "sem saída compatível: nenhuma trava"


def test_alert_in_pt_br(pt_br, tmp_path):
    from ke.messages import render_guard_chunks

    ((text, _),) = render_guard_chunks(
        [{"board": "b", "task_id": "t_1", "reason": "active_pr", "age_s": 2000, "title": "x",
          "assignee": "dev", "pr_url": PR, "pr_at": 1_700_000_000}]
    )
    assert "hermes kanban-empowered release <id> --to <profile>" in text
    assert "travado" in text


# -- CLI --------------------------------------------------------------------------
def _cli_settings(tmp_path):
    return settings_for_test(tmp_path)


def test_cli_release_parses():
    import argparse

    from ke import cli

    p = argparse.ArgumentParser()
    cli.setup(p)
    a = p.parse_args(["release", "t_1", "--to", "dev", "--board", "matrix"])
    assert (a.ke_cmd, a.card, a.to, a.board) == ("release", "t_1", "dev", "matrix")
    assert p.parse_args(["release", "t_1", "--to", "dev"]).board is None


def test_cli_release_prints_and_exits_zero(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, hermes_api

    monkeypatch.setattr(hermes_api, "profiles", lambda: ["default", "dev", "reviewer"])
    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    assert cli.run_release(_cli_settings(tmp_path), tid, "reviewer", None) == 0
    assert "released: assigned to reviewer" in capsys.readouterr().out
    assert _assignee("default", tid) == "reviewer"
    store = _store(tmp_path)
    assert [a["kind"] for a in store.actions_since(0)] == ["guard_release"]
    assert store.actions_since(0)[0]["detail"].startswith("cli:")


def test_cli_release_failure_exits_one_and_changes_nothing(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, hermes_api

    monkeypatch.setattr(hermes_api, "profiles", lambda: ["default", "dev", "reviewer"])
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="calm", assignee="dev")
    assert cli.run_release(_cli_settings(tmp_path), tid, "reviewer", "default") == 1
    assert "no compatible exit: no guard" in capsys.readouterr().out
    assert _assignee("default", tid) == "dev"
    assert _store(tmp_path).actions_since(0) == []


def test_cli_release_unknown_card(kanban_home, tmp_path, capsys):
    from ke import cli

    assert cli.run_release(_cli_settings(tmp_path), "t_nope", "dev", None) == 1
    assert "not found" in capsys.readouterr().out


def test_cli_run_dispatches_release(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, hermes_api

    monkeypatch.setattr(hermes_api, "profiles", lambda: ["default", "dev", "reviewer"])
    cli.bind(_cli_settings(tmp_path))
    with hermes_api.connect("default") as conn:
        tid = _guarded_card(conn)
    args = types.SimpleNamespace(ke_cmd="release", card=tid, to="reviewer", board=None)
    assert cli.run(args) == 0
