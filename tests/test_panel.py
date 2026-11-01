"""Task 12: the live panel (Snapshot), its pinned Telegram message and ``status --watch``."""
from __future__ import annotations

import argparse
import datetime
import json
import time

import pytest

from conftest import block_task, create_task, make_coordinator
from test_conditions import _claim, _raw_event
from test_inbound import reply_event
from test_waits import make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

OWNER = 6802889854
BOSS = 5550001
EDIT_GONE = "Bad Request: message to edit not found"
EDIT_LOCKED = "Bad Request: message can't be edited"


# -- helpers -------------------------------------------------------------------
def _item(n=1, board="matrix", need="plug the board in", age_s=7500, held=3, version=11, owner="sponsor"):
    return {
        "board": board, "task_id": f"t_{n:08x}", "title": "title", "owner": owner, "age_s": age_s,
        "need": need, "version": version, "held": held,
    }


def snapshot_fixture(**kw):
    from ke.panel import Snapshot

    base = dict(
        ts=time.mktime((2026, 10, 31, 14, 5, 0, 0, 0, -1)),
        running=[{"board": "matrix", "task_id": "t_r1", "title": "build", "assignee": "dev", "age_s": 600,
                  "heartbeat_age_s": 20}],
        held=[{"board": "matrix", "task_id": "t_g1", "reason": "active_pr", "age_s": 4000}],
        human_waits=[_item(1), _item(2, board="side", need="approve the deploy", age_s=300, held=0, version=12)],
        resolved_today=4,
        last_resolved={"board": "matrix", "task_id": "t_done1", "kind": "human_done",
                       "ts": time.mktime((2026, 10, 31, 13, 40, 0, 0, 0, -1))},
        long_guards=1,
    )
    base.update(kw)
    return Snapshot(**base)


class PanelApi:
    """``hermes_api`` as the coordinator sees it (the unpatched Hermes under test would warn about hooks)."""

    def __init__(self):
        from ke import hermes_api

        self._api = hermes_api

    def veto_hooks_available(self):
        return True

    def __getattr__(self, name):
        return getattr(self._api, name)


def _coord(tmp_path, clock, fake_tg, **kw):
    kw.setdefault("owner_ids", {"sponsor": OWNER})
    kw.setdefault("pin_live_panel", True)
    return make_coordinator(tmp_path, clock=clock, telegram=fake_tg, api=PanelApi(), **kw)


def _kv(c, key):
    return c.store.kv_get(key)


@pytest.fixture
def coord(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    return _coord(tmp_path, clock, fake_tg)


def _block(title="card", reason="Preciso: plug it in\nPor quê: x\nComo responder: y", **kw):
    from ke import hermes_api

    with hermes_api.connect(kw.pop("board", "default")) as conn:
        tid, ev = make_human_block(conn, reason=reason, title=title, **kw)
    return tid, int(ev.id)


def _unblock(tid, board="default"):
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        assert hermes_api.unblock(conn, tid)


def change_state():
    """Any visible change of the fleet: one more card now waits on the human."""
    return _block(title="a new blocker")


# -- render ---------------------------------------------------------------------
def test_render_contains_spec_sections():
    from ke.panel import render_telegram

    text = render_telegram(snapshot_fixture())
    for s in ("Kanban now", "Needs you", "Resolved on its own today", 'Reply "1 done"'):
        assert s in text


def test_render_title_counts_items_and_guards():
    from ke.panel import render_telegram

    lines = render_telegram(snapshot_fixture()).splitlines()
    assert lines[0] == "Kanban now · 14:05"
    assert lines[1] == "1 running · 1 held · 2 depend on you"
    text = "\n".join(lines)
    assert "1. matrix: plug the board in" in text
    assert "2. side: approve the deploy" in text
    assert "2 h 5 min" in text and "holding 3" in text and "holding 0" in text
    assert "Resolved on its own today: 4" in text
    assert "t_done1" in text and "13:40" in text
    assert "Long respawn guards: 1" in text  # the long-guards line
    assert lines[-1].startswith('Reply "1 done"')


def test_render_without_waits_resolved_or_guards():
    from ke.panel import render_telegram

    text = render_telegram(snapshot_fixture(human_waits=[], held=[], running=[], last_resolved=None,
                                            resolved_today=0, long_guards=0))
    assert "Needs you" not in text and "guard" not in text.lower() and "Reply" not in text
    assert "Nothing needs you right now." in text
    assert "Resolved on its own today: 0" in text


def test_render_in_pt_br(pt_br):  # noqa: F811
    from ke.panel import render_telegram

    text = render_telegram(snapshot_fixture())
    for s in ("Kanban agora · 14:05", "Precisa de você", "Resolvido sozinho hoje: 4", 'Responda "1 feito"'):
        assert s in text


def test_render_clips_long_lists_to_telegram_limit():
    from ke.messages import TELEGRAM_LIMIT, _tg_len
    from ke.panel import render_telegram, shown_waits

    waits = [_item(i, need="x" * 400, age_s=1000 + i) for i in range(1, 60)]
    snap = snapshot_fixture(human_waits=waits)
    text = render_telegram(snap)
    assert _tg_len(text) <= TELEGRAM_LIMIT
    shown = shown_waits(snap)
    assert 0 < len(shown) < len(waits)
    assert f"{len(waits) - len(shown)} more" in text
    assert text.count("\n1. ") == 1 and f"\n{len(shown)}. " in text


def test_shown_waits_oldest_first_and_stable():
    from ke.panel import shown_waits

    snap = snapshot_fixture(human_waits=[_item(1, age_s=10), _item(2, age_s=900), _item(3, age_s=900)])
    assert [w["task_id"] for w in shown_waits(snap)] == ["t_00000002", "t_00000003", "t_00000001"]


def test_render_terminal_has_rows_and_no_reply_hint():
    from ke.panel import render_terminal

    text = render_terminal(snapshot_fixture())
    assert "Kanban now · 14:05" in text
    assert "matrix/t_r1" in text and "matrix/t_g1" in text  # running and held rows
    assert "1. matrix: plug the board in" in text
    assert "Resolved on its own today: 4" in text
    assert "Reply" not in text and "hermes kanban-empowered done" in text


# -- build_snapshot ---------------------------------------------------------------
def test_build_snapshot_collects_everything(kanban_home, tmp_path, clock):
    from ke import conditions, hermes_api as h
    from ke.panel import build_snapshot
    from ke.store import Store

    store = Store(tmp_path / "state" / "state.db")
    now = time.time()
    with h.connect("default") as conn:
        run_t = create_task(conn, title="running one", assignee="dev")
        _claim(conn, run_t)
        wait_t, ev = make_human_block(conn, reason="Preciso: ligue a placa\nPor quê: b\nComo responder: c",
                                      title="needs a human")
        held = create_task(conn, title="behind", parents=[wait_t])
        guard_t = create_task(conn, title="guarded", assignee="dev")
        _raw_event(conn, guard_t, "respawn_guarded", {"reason": "active_pr"})
    store.upsert_guard_alert("default", "tracked", "active_pr", int(now) - 4000)  # held: guard_alerts only
    midnight = conditions._local_midnight(now)
    store.log_action("default", run_t, "human_done", None, midnight + 5)
    store.log_action("default", run_t, "guard_release", None, midnight + 6)
    store.log_action("default", run_t, "infra_undo", None, midnight + 7)
    store.log_action("default", run_t, "human_done", None, midnight - 10)  # yesterday
    store.log_action("default", run_t, "escalation_copy_failed", "x", midnight + 8)  # not a resolution
    snap = build_snapshot(store, h, now + 1)
    assert [r["task_id"] for r in snap.running] == [run_t]
    assert [g["task_id"] for g in snap.held] == ["tracked"]  # the ready+respawn_guarded card is NOT used
    assert snap.held[0]["age_s"] >= 4000
    assert len(snap.human_waits) == 1
    w = snap.human_waits[0]
    assert (w["task_id"], w["version"], w["need"], w["held"]) == (wait_t, int(ev.id), "ligue a placa", 1)
    assert snap.resolved_today == 3
    assert snap.last_resolved["kind"] == "infra_undo" and snap.last_resolved["task_id"] == run_t
    assert snap.long_guards == 1
    assert snap.ts == now + 1 and held


def test_build_snapshot_unstructured_reason_and_no_actions(kanban_home, tmp_path):
    from ke import hermes_api as h
    from ke.panel import build_snapshot
    from ke.store import Store

    store = Store(tmp_path / "state" / "state.db")
    with h.connect("default") as conn:
        make_human_block(conn, reason="please look at the logs", title="x")
    snap = build_snapshot(store, h, time.time() + 1)
    assert snap.human_waits[0]["need"] == "please look at the logs"
    assert snap.resolved_today == 0 and snap.last_resolved is None and snap.long_guards == 0


def test_build_snapshot_short_guards_are_not_long(kanban_home, tmp_path):
    from ke import hermes_api as h
    from ke.panel import build_snapshot
    from ke.store import Store

    store = Store(tmp_path / "state" / "state.db")
    now = time.time()
    store.upsert_guard_alert("default", "a", "active_pr", int(now) - 60)
    store.upsert_guard_alert("default", "b", "active_pr", int(now) - 3600)
    snap = build_snapshot(store, h, now)
    assert len(snap.held) == 2 and snap.long_guards == 1


# -- pinned message -----------------------------------------------------------------
def test_panel_edits_only_when_changed(coord, fake_tg, clock):
    coord.update_panel(clock.now)
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and fake_tg.count("edit") == 0 and fake_tg.count("pin") == 1


def test_panel_first_send_pins_and_stores_id_chat_and_hash(coord, fake_tg, clock):
    coord.update_panel(clock.now)
    msg = fake_tg.calls["send"][0]["message_id"]
    assert fake_tg.calls["send"][0]["chat_id"] == OWNER
    assert fake_tg.calls["pin"][0]["message_id"] == msg
    assert _kv(coord, "panel_message_id") == str(msg)
    assert _kv(coord, "panel_hash")


def test_panel_edits_when_the_text_changed(coord, fake_tg, clock):
    coord.update_panel(clock.now)
    change_state()
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and fake_tg.count("edit") == 1
    assert fake_tg.calls["edit"][0]["message_id"] == fake_tg.calls["send"][0]["message_id"]
    assert "Needs you" in fake_tg.calls["edit"][0]["text"]
    coord.update_panel(clock.now)
    assert fake_tg.count("edit") == 1  # unchanged again


@pytest.mark.parametrize("description", [EDIT_GONE, EDIT_LOCKED])
def test_panel_recreated_when_message_gone(coord, fake_tg, clock, description):
    from ke.telegram import TelegramError

    coord.update_panel(clock.now)
    first = fake_tg.calls["send"][0]["message_id"]
    change_state()
    fake_tg.fail_next_edit = TelegramError(400, description)
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 2 and fake_tg.count("pin") == 2
    second = fake_tg.calls["send"][1]["message_id"]
    assert second != first and fake_tg.calls["pin"][1]["message_id"] == second
    assert _kv(coord, "panel_message_id") == str(second)
    coord.update_panel(clock.now)  # the new message is now the one being tracked
    assert fake_tg.count("send") == 2 and fake_tg.count("edit") == 0


@pytest.mark.parametrize("error", [None, 429, 500, 403, 400])
def test_panel_other_edit_errors_keep_the_id(coord, fake_tg, clock, caplog, error):
    import logging

    from ke.telegram import TelegramError

    coord.update_panel(clock.now)
    first = _kv(coord, "panel_message_id")
    change_state()
    fake_tg.fail_next_edit = TelegramError(error, "Bad Request: something else" if error == 400 else "boom")
    with caplog.at_level(logging.WARNING):
        coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and fake_tg.count("pin") == 1
    assert _kv(coord, "panel_message_id") == first
    assert caplog.records  # logged, not silent
    coord._tg_down = False  # a new evaluation resets the transport flag
    coord.update_panel(clock.now)  # and the next cycle simply retries the edit
    assert fake_tg.count("edit") == 1 and _kv(coord, "panel_message_id") == first


def test_panel_message_not_modified_counts_as_done(coord, fake_tg, clock):
    from ke.telegram import TelegramError

    coord.update_panel(clock.now)
    change_state()
    fake_tg.fail_next_edit = TelegramError(
        400, "Bad Request: message is not modified: specified new message content and reply markup are exactly the same"
    )
    coord.update_panel(clock.now)
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and fake_tg.count("edit") == 0  # hash was stored: nothing to retry


def test_panel_send_failure_keeps_state_and_never_pins(coord, fake_tg, clock):
    from ke.telegram import TelegramError

    fake_tg.fail_next = TelegramError(None, "network down")
    coord.update_panel(clock.now)
    assert fake_tg.count("pin") == 0 and _kv(coord, "panel_message_id") is None
    assert coord._tg_down is True
    coord.evaluate(clock.now)  # next evaluation resets the flag and tries again
    assert fake_tg.count("send") == 1 and _kv(coord, "panel_message_id") is not None


def test_panel_pin_failure_keeps_the_message_and_does_not_spam(coord, fake_tg, clock):
    from ke.telegram import TelegramError

    fake_tg.fail_next_pin = TelegramError(400, "Bad Request: not enough rights to pin a message")
    coord.update_panel(clock.now)
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and _kv(coord, "panel_message_id") is not None


def test_panel_transient_pin_failure_is_retried_later(coord, fake_tg, clock):
    from ke.telegram import TelegramError

    fake_tg.fail_next_pin = TelegramError(None, "timeout")
    coord.update_panel(clock.now)
    assert fake_tg.count("pin") == 0 and coord._tg_down is True
    coord._tg_down = False  # a new evaluation
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 1 and fake_tg.count("pin") == 1
    coord.update_panel(clock.now)
    assert fake_tg.count("pin") == 1  # pinned: done


def test_panel_transport_down_blocks_all_panel_traffic(coord, fake_tg, clock):
    coord._tg_down = True
    coord.update_panel(clock.now)
    assert fake_tg.count("send") == 0


def test_panel_edit_transport_failure_marks_telegram_down(coord, fake_tg, clock):
    from ke.telegram import TelegramError

    coord.update_panel(clock.now)
    change_state()
    fake_tg.fail_next_edit = TelegramError(None, "timeout")
    coord.update_panel(clock.now)
    assert coord._tg_down is True and fake_tg.count("send") == 1


def test_panel_disabled_by_setting(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    c = _coord(tmp_path, clock, fake_tg, pin_live_panel=False)
    c.evaluate(clock.now)
    assert fake_tg.count("send") == 0 and fake_tg.count("pin") == 0


def test_panel_without_bot_token_or_chat_is_silent(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    no_tg = make_coordinator(
        tmp_path / "a", clock=clock, telegram=None, api=PanelApi(), owner_ids={"sponsor": OWNER}, pin_live_panel=True
    )
    no_tg.evaluate(clock.now)  # must not raise
    no_chat = _coord(tmp_path / "b", clock, fake_tg, owner_ids={})
    no_chat.evaluate(clock.now)
    assert fake_tg.count("send") == 0


def test_panel_goes_to_the_default_owner(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    c = _coord(tmp_path, clock, fake_tg, owner_ids={"sponsor": OWNER, "boss": BOSS}, default_owner="boss")
    c.update_panel(clock.now)
    assert fake_tg.calls["send"][0]["chat_id"] == BOSS and fake_tg.calls["pin"][0]["chat_id"] == BOSS


def test_panel_recreated_when_the_owner_chat_changes(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    c = _coord(tmp_path, clock, fake_tg, owner_ids={"sponsor": OWNER})
    c.update_panel(clock.now)
    import dataclasses

    c.settings = dataclasses.replace(c.settings, owner_ids={"sponsor": BOSS})
    c.update_panel(clock.now)
    assert fake_tg.count("send") == 2 and fake_tg.calls["send"][1]["chat_id"] == BOSS and fake_tg.count("edit") == 0


def test_panel_step_runs_in_evaluate_after_the_others(coord, fake_tg, clock):
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 1 and "Kanban now" in fake_tg.last_text


def test_panel_step_failure_is_contained(coord, fake_tg, clock, monkeypatch, caplog):
    from ke import panel

    monkeypatch.setattr(panel, "build_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    coord.evaluate(clock.now)  # must not raise
    assert fake_tg.count("send") == 0


def test_panel_recorded_as_sent_message_with_numbered_items(coord, fake_tg, clock):
    t1, v1 = _block(title="one", reason="Preciso: first\nPor quê: x\nComo responder: y")
    coord.update_panel(clock.now)
    msg = fake_tg.calls["send"][0]["message_id"]
    sent = coord.store.get_sent_message(OWNER, msg)
    assert sent["kind"] == "panel"
    assert sent["items"] == [{"board": "default", "task_id": t1, "version": v1}]


def test_panel_items_follow_every_edit_and_keep_a_short_history(coord, fake_tg, clock):
    t1, v1 = _block(title="one")
    coord.update_panel(clock.now)
    msg = fake_tg.calls["send"][0]["message_id"]
    clock.advance(60)
    t2, v2 = _block(title="two")
    coord.update_panel(clock.now)
    sent = coord.store.get_sent_message(OWNER, msg)
    assert {i["task_id"] for i in sent["items"]} == {t1, t2}
    assert sent["created_at"] == int(clock.now)
    from ke.panel import load_history

    hist = load_history(coord.store, OWNER, msg)
    assert [len(e["items"]) for e in hist["entries"]] == [1, 2] and hist["truncated"] is False
    clock.advance(60)
    _unblock(t1)
    coord.update_panel(clock.now)
    hist = load_history(coord.store, OWNER, msg)
    assert [len(e["items"]) for e in hist["entries"]] == [2, 1] and hist["truncated"] is True


def test_items_resync_without_edit_when_only_the_version_changed(coord, fake_tg, clock):
    t1, v1 = _block(title="one")
    coord.update_panel(clock.now)
    msg = fake_tg.calls["send"][0]["message_id"]
    # same text, other version: no Telegram call, but replies must be validated against the new one
    coord.store.add_sent_message(msg, OWNER, "panel", [{"board": "default", "task_id": t1, "version": v1 - 1}], int(clock.now))
    coord.update_panel(clock.now)
    assert fake_tg.count("edit") == 0
    assert coord.store.get_sent_message(OWNER, msg)["items"][0]["version"] == v1


# -- replies to the panel ----------------------------------------------------------------
class Rig:
    def __init__(self, coord, fake_tg, clock):
        self.c, self.tg, self.clock = coord, fake_tg, clock

    def handle(self, user, text, at, number_reply_to=None, chat=OWNER, **kw):
        from ke.inbound import handle_inbound

        ev = reply_event(user, number_reply_to if number_reply_to is not None else self.msg, text, chat_id=chat, **kw)
        ev.timestamp = datetime.datetime.fromtimestamp(at)
        return handle_inbound(ev, settings=self.c.settings, store=self.c.store, api=self.c.api,
                              telegram=self.tg, now=self.clock.now)

    @property
    def msg(self):
        return self.tg.calls["send"][0]["message_id"]

    @staticmethod
    def status(tid, board="default"):
        from ke import hermes_api

        with hermes_api.connect(board) as conn:
            return hermes_api.task(conn, tid).status


@pytest.fixture
def rig(coord, fake_tg, clock):
    return Rig(coord, fake_tg, clock)


def test_numbered_done_on_the_panel_unblocks_that_item(rig, fake_tg, clock):
    t1, _ = _block(title="one")
    t2, _ = _block(title="two")
    rig.c.update_panel(clock.now)
    items = rig.c.store.get_sent_message(OWNER, rig.msg)["items"]
    n = [i["task_id"] for i in items].index(t2) + 1
    assert rig.handle(str(OWNER), f"{n} done", clock.now + 1)["action"] == "skip"
    assert rig.status(t2) != "blocked" and rig.status(t1) == "blocked"
    assert fake_tg.calls["send"][-1]["reply_to"] == rig.msg or "Unblocked" in fake_tg.last_text


def test_bare_done_on_a_single_item_panel(rig, clock):
    t1, _ = _block(title="one")
    rig.c.update_panel(clock.now)
    assert rig.handle(str(OWNER), "done", clock.now + 1)["action"] == "skip"
    assert rig.status(t1) != "blocked"


def test_bare_done_on_a_multi_item_panel_asks_which(rig, fake_tg, clock):
    t1, _ = _block(title="one")
    t2, _ = _block(title="two")
    rig.c.update_panel(clock.now)
    assert rig.handle(str(OWNER), "done", clock.now + 1)["action"] == "skip"
    assert rig.status(t1) == "blocked" and rig.status(t2) == "blocked"
    assert "which item" in fake_tg.last_text.lower()


def test_stranger_and_non_owner_cannot_resolve_from_the_panel(kanban_home, tmp_path, clock, fake_tg):
    clock.now = time.time()
    c = _coord(tmp_path, clock, fake_tg, owner_ids={"sponsor": OWNER, "boss": BOSS})
    t1, _ = _block(title="one")
    c.update_panel(clock.now)
    r = Rig(c, fake_tg, clock)
    assert r.handle("999", "1 done", clock.now + 1) is None
    assert r.handle(str(BOSS), "1 done", clock.now + 1) is None  # known identity, not the owner of the item
    assert r.status(t1) == "blocked" and fake_tg.count("send") == 1


def test_panel_item_of_another_owner_passes_through(kanban_home, tmp_path, clock, fake_tg):
    from hermes_cli import kanban_db_boards as kbb

    kbb.create_board("side")
    clock.now = time.time()
    c = _coord(tmp_path, clock, fake_tg, owner_ids={"sponsor": OWNER, "boss": BOSS}, board_owner={"side": "boss"})
    mine, _ = _block(title="mine")
    theirs, _ = _block(title="theirs", board="side")
    c.update_panel(clock.now)
    items = c.store.get_sent_message(OWNER, fake_tg.calls["send"][0]["message_id"])["items"]
    n_theirs = [i["task_id"] for i in items].index(theirs) + 1
    n_mine = [i["task_id"] for i in items].index(mine) + 1
    r = Rig(c, fake_tg, clock)
    assert r.handle(str(OWNER), f"{n_theirs} done", clock.now + 1) is None  # not the sender's item
    assert r.status(theirs, "side") == "blocked"
    assert r.handle(str(OWNER), "done", clock.now + 1) is None  # bare reply: the sender does not own every item
    assert r.handle(str(OWNER), f"{n_mine} done", clock.now + 1)["action"] == "skip"
    assert r.status(mine) != "blocked" and r.status(theirs, "side") == "blocked"


def test_replay_of_a_panel_reply_is_consumed_once(rig, fake_tg, clock):
    t1, _ = _block(title="one")
    rig.c.update_panel(clock.now)
    assert rig.handle(str(OWNER), "1 done", clock.now + 1, message_id="9001")["action"] == "skip"
    sends = fake_tg.count("send")
    assert rig.handle(str(OWNER), "1 done", clock.now + 1, message_id="9001")["action"] == "skip"
    assert fake_tg.count("send") == sends


def test_reblocked_card_is_not_resolved_by_an_old_panel_reply(rig, fake_tg, clock):
    t1, _ = _block(title="one")
    rig.c.update_panel(clock.now)
    from test_inbound import _reblock

    _reblock(t1)
    assert rig.handle(str(OWNER), "1 done", clock.now + 1)["action"] == "skip"
    assert rig.status(t1) == "blocked" and "changed" in fake_tg.last_text


def test_expired_panel_reply(rig, fake_tg, clock):
    t1, _ = _block(title="one")
    rig.c.update_panel(clock.now)
    clock.advance(rig.c.settings.reply_ttl_s + 1)
    rig.handle(str(OWNER), "1 done", clock.now)
    assert rig.status(t1) == "blocked" and "expired" in fake_tg.last_text


def _two_edits(rig, clock):
    """Panel shows [A, B] at t0, then [B] at t0 + 60 (A was resolved elsewhere)."""
    a, va = _block(title="a-old", reason="Preciso: aaa\nPor quê: x\nComo responder: y")
    b, vb = _block(title="b-new", reason="Preciso: bbb\nPor quê: x\nComo responder: y")
    # A has been waiting longer, so it is item 1 (the events themselves share the same second)
    rig.c.store.upsert_wait("default", a, va, "sponsor", int(clock.now) - 3600)
    rig.c.store.upsert_wait("default", b, vb, "sponsor", int(clock.now) - 60)
    t0 = clock.now
    rig.c.update_panel(t0)
    items = rig.c.store.get_sent_message(OWNER, rig.msg)["items"]
    assert [i["task_id"] for i in items] == [a, b]  # oldest first
    _unblock(a)
    clock.now = t0 + 60
    rig.c.update_panel(clock.now)
    assert [i["task_id"] for i in rig.c.store.get_sent_message(OWNER, rig.msg)["items"]] == [b]
    return a, b, t0


def test_reply_written_before_the_edit_refers_to_the_list_the_user_saw(rig, fake_tg, clock):
    a, b, t0 = _two_edits(rig, clock)
    clock.now = t0 + 90  # processed late; the reply is dated before the edit: "1" meant A
    assert rig.handle(str(OWNER), "1 done", t0 + 30)["action"] == "skip"
    assert rig.status(b) == "blocked"  # B (now position 1) was NOT resolved by mistake
    assert "changed" in fake_tg.last_text


def test_reply_long_after_the_edit_uses_the_current_list(rig, fake_tg, clock):
    a, b, t0 = _two_edits(rig, clock)
    clock.now = t0 + 100
    assert rig.handle(str(OWNER), "1 done", t0 + 100)["action"] == "skip"
    assert rig.status(b) != "blocked"


def test_reply_right_after_an_edit_that_moved_the_item_is_ambiguous(rig, fake_tg, clock):
    a, b, t0 = _two_edits(rig, clock)
    clock.now = t0 + 70  # 10 s after the edit: the user may still have seen [A, B]
    assert rig.handle(str(OWNER), "1 done", t0 + 70)["action"] == "skip"
    assert rig.status(b) == "blocked"
    assert "panel changed" in fake_tg.last_text.lower()
    # a position that exists in neither list unambiguously is just as ambiguous
    assert rig.handle(str(OWNER), "2 done", t0 + 70)["action"] == "skip"
    assert rig.status(b) == "blocked"


def test_ambiguity_reply_is_in_pt_br(rig, fake_tg, clock, pt_br):  # noqa: F811
    a, b, t0 = _two_edits(rig, clock)
    clock.now = t0 + 70
    rig.handle(str(OWNER), "1 feito", t0 + 70)
    assert "painel mudou" in fake_tg.last_text.lower() and rig.status(b) == "blocked"


def test_reply_right_after_an_edit_is_fine_when_the_position_did_not_change(rig, fake_tg, clock):
    a, va = _block(title="a", reason="Preciso: aaa\nPor quê: x\nComo responder: y")
    rig.c.store.upsert_wait("default", a, va, "sponsor", int(clock.now) - 3600)  # A waits longest: item 1
    clock.advance(10)
    t0 = clock.now
    rig.c.update_panel(t0)
    b, vb = _block(title="b", reason="Preciso: bbb\nPor quê: x\nComo responder: y")
    rig.c.store.upsert_wait("default", b, vb, "sponsor", int(clock.now) - 30)
    clock.now = t0 + 60
    rig.c.update_panel(clock.now)  # [A] -> [A, B]; item 1 is A in both
    clock.now = t0 + 65
    assert rig.handle(str(OWNER), "1 done", t0 + 65)["action"] == "skip"
    assert rig.status(a) != "blocked" and rig.status(b) == "blocked"


def test_unauthenticated_reply_to_a_changed_panel_stays_untouched(rig, fake_tg, clock):
    a, b, t0 = _two_edits(rig, clock)
    clock.now = t0 + 70
    sends = fake_tg.count("send")
    assert rig.handle("999", "1 done", t0 + 70) is None  # ambiguous or not, a stranger gets nothing
    assert fake_tg.count("send") == sends


def test_reply_older_than_the_kept_history_is_ambiguous(rig, fake_tg, clock):
    a, _ = _block(title="a", reason="Preciso: aaa\nPor quê: x\nComo responder: y")
    t0 = clock.now
    rig.c.update_panel(t0)
    ids = [a]
    for k in (1, 2):  # two more edits push the first list out of the history
        ids.append(_block(title=f"x{k}")[0])
        clock.now = t0 + 100 * k
        rig.c.update_panel(clock.now)
    clock.now = t0 + 500
    assert rig.handle(str(OWNER), "1 done", t0 + 10)["action"] == "skip"  # dated before anything we kept
    assert all(rig.status(i) == "blocked" for i in ids)
    assert "panel changed" in fake_tg.last_text.lower()


def test_panel_sent_message_without_history_uses_its_items(coord, fake_tg, clock):
    t1, v1 = _block(title="one")
    coord.store.add_sent_message(777, OWNER, "panel", [{"board": "default", "task_id": t1, "version": v1}], int(clock.now))
    r = Rig(coord, fake_tg, clock)
    assert r.handle(str(OWNER), "1 done", clock.now + 1, number_reply_to=777)["action"] == "skip"
    assert r.status(t1) != "blocked"


def test_old_panel_messages_stay_answerable_with_their_last_items(rig, fake_tg, clock):
    from ke.telegram import TelegramError

    t1, _ = _block(title="one")
    rig.c.update_panel(clock.now)
    first = rig.msg
    change_state()
    fake_tg.fail_next_edit = TelegramError(400, EDIT_GONE)
    rig.c.update_panel(clock.now)  # new message; the old one keeps its last items
    assert rig.handle(str(OWNER), "1 done", clock.now + 1, number_reply_to=first)["action"] == "skip"
    assert rig.status(t1) != "blocked"


# -- status --watch ------------------------------------------------------------------
def test_status_parser_accepts_watch():
    from ke import cli

    p = argparse.ArgumentParser()
    cli.setup(p)
    assert p.parse_args(["status", "--watch"]).watch is True
    assert p.parse_args(["status"]).watch is False


def test_status_watch_rerenders_every_5s_and_exits_cleanly_on_ctrl_c(kanban_home, tmp_path, capsys):
    from ke import cli
    from conftest import settings_for_test

    clears, sleeps = [], []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    rc = cli.run_status_watch(settings_for_test(tmp_path), sleep=sleep, clear=lambda: clears.append(1))
    out = capsys.readouterr().out
    assert rc == 0 and sleeps == [5, 5, 5] and len(clears) == 3
    assert out.count("Kanban now") == 3 and "Ctrl+C" in out


def test_status_watch_interrupt_during_render_is_clean(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, panel
    from conftest import settings_for_test

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(panel, "build_snapshot", boom)
    assert cli.run_status_watch(settings_for_test(tmp_path), sleep=lambda s: None, clear=lambda: None) == 0


def test_status_watch_survives_a_render_error(kanban_home, tmp_path, capsys, monkeypatch):
    from ke import cli, panel
    from conftest import settings_for_test

    calls = []
    real = panel.build_snapshot

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db locked")
        return real(*a, **k)

    monkeypatch.setattr(panel, "build_snapshot", flaky)
    n = []

    def sleep(s):
        n.append(1)
        if len(n) == 2:
            raise KeyboardInterrupt

    assert cli.run_status_watch(settings_for_test(tmp_path), sleep=sleep, clear=lambda: None) == 0
    out = capsys.readouterr().out
    assert "db locked" in out and "Kanban now" in out


def test_clear_screen_uses_cls_on_windows_and_ansi_elsewhere(monkeypatch, capsys):
    import os

    from ke import cli

    called = []
    monkeypatch.setattr(os, "system", lambda cmd: called.append(cmd) or 0)
    monkeypatch.setattr(os, "name", "nt")
    cli._clear_screen()
    assert called == ["cls"]
    monkeypatch.setattr(os, "name", "posix")
    cli._clear_screen()
    assert called == ["cls"] and "\x1b[2J" in capsys.readouterr().out


def test_run_dispatches_watch(monkeypatch, tmp_path):
    from ke import cli
    from conftest import settings_for_test

    seen = []
    monkeypatch.setattr(cli, "run_status_watch", lambda settings, **kw: seen.append("watch") or 0)
    monkeypatch.setattr(cli, "run_status", lambda settings: seen.append("once") or 0)
    cli.bind(settings_for_test(tmp_path))
    ns = argparse.Namespace
    assert cli.run(ns(ke_cmd="status", watch=True)) == 0
    assert cli.run(ns(ke_cmd="status", watch=False)) == 0
    assert seen == ["watch", "once"]


def test_fleet_status_accepts_an_api(tmp_path):
    from ke import conditions
    from ke.store import Store

    class NoBoards:
        def boards(self):
            return []

    fs = conditions.fleet_status(Store(tmp_path / "s.db"), time.time(), api=NoBoards())
    assert fs["boards"] == [] and fs["running"] == []
