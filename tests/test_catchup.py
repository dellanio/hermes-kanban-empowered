"""Live issue 01/11/2026: waits that got one old-format message and then silence.

1. a template version bump re-sends every open human wait once, without moving its stage;
2. an already-old wait never discovered before gets a first message now and then a daily cadence;
3. reaching the escalation stage with nobody to escalate to raises ONE setup alert;
4. ``waits.blocking_digest_every`` sets the digest cadence of waits that hold other cards.
"""
from __future__ import annotations

import json

import pytest

from conftest import create_task, make_coordinator, settings_for_test
from test_inbound import OWNER, reply_event
from test_waits import _start, make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

H = 3600
D = 86400
OWNERS = {"sponsor": int(OWNER), "ops": 222}


def _coord(tmp_path, fake_tg, clock, **kw):
    kw.setdefault("owner_ids", dict(OWNERS))
    c = make_coordinator(tmp_path, clock=clock, telegram=fake_tg, api=_api(), **kw)
    # the real Hermes here has no veto hooks: mute that warning, it is not what these tests are about
    c.store.kv_set("veto_hooks_warned", "1")
    c.store.kv_set("veto_hooks_warn_sent", "1")
    return c


def _api():
    from ke import hermes_api

    return hermes_api


def _blocks(n=1, **kw):
    """``n`` human-blocked cards on the default board; returns ([task ids], [events])."""
    tids, evs = [], []
    with _api().connect("default") as conn:
        for i in range(n):
            tid, ev = make_human_block(
                conn, reason=f"Preciso: acao {i}\nPor quê: motivo\nComo responder: feito", title=f"card {i}", **kw
            )
            tids.append(tid)
            evs.append(ev)
    return tids, evs


def _sent_rows(c):
    return [dict(r) for r in c.store.conn.execute("SELECT * FROM sent_messages ORDER BY message_id")]


def _texts(fake_tg):
    return [m["text"] for m in fake_tg.calls["send"]]


def _reminders(fake_tg):
    return [x for x in _texts(fake_tg) if "NEEDS YOU" in x]


def _setups(fake_tg):
    return [x for x in _texts(fake_tg) if "⚙️" in x]


def _pretend_old_send(c, tids, evs, stage="escalation", at=None):
    """The state the live install was in: each wait already got an (old-format) message at ``stage``."""
    for tid, ev in zip(tids, evs):
        c.store.upsert_wait("default", tid, ev.id, "sponsor", ev.created_at)
        c.store.mark_wait_sent("default", tid, ev.id, stage, int(at if at is not None else ev.created_at))


# -- 1. template version resend -----------------------------------------------------------------
def test_template_version_is_a_constant_of_messages():
    from ke import messages

    assert isinstance(messages.MESSAGE_TEMPLATE_VERSION, int) and messages.MESSAGE_TEMPLATE_VERSION >= 2


def test_version_change_resends_open_waits_once_grouped_and_keeps_stages(kanban_home, fake_tg, clock, tmp_path):
    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(2)
    _start(clock, evs[-1])
    clock.advance(9 * H)  # past the escalation: stage "escalation" was sent in the old format
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    c.evaluate(clock.now)
    (text,) = _reminders(fake_tg)  # one grouped message, normal numbering and footer
    assert "① " in text and "② " in text and "When replying, name the item" in text
    assert text.startswith("[Kanban - Empowered]\n")
    (row,) = _sent_rows(c)
    assert row["kind"] == "waits"
    assert {i["task_id"] for i in json.loads(row["items_json"])} == set(tids)
    for tid, ev in zip(tids, evs):  # stages untouched: the resend is not a stage
        w = c.store.latest_wait("default", tid)
        assert w["stage"] == "escalation" and w["last_sent_at"] == int(clock.now - 60)
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)
    for _ in range(4):  # never again
        clock.advance(31)
        c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1


def test_a_new_bump_resends_again_once(kanban_home, fake_tg, clock, tmp_path, monkeypatch):
    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1
    monkeypatch.setattr(messages, "MESSAGE_TEMPLATE_VERSION", messages.MESSAGE_TEMPLATE_VERSION + 1)
    clock.advance(31)
    c.evaluate(clock.now)
    clock.advance(31)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 2
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)


def test_replies_to_the_resend_resolve_the_wait(kanban_home, fake_tg, clock, tmp_path):
    from ke import hermes_api
    from ke.inbound import handle_inbound

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(2)
    _start(clock, evs[-1])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    c.evaluate(clock.now)
    (call,) = [m for m in fake_tg.calls["send"] if "NEEDS YOU" in m["text"]]
    out = handle_inbound(
        reply_event(user_id=OWNER, reply_to=call["message_id"], text="2 — feito"),
        settings=c.settings, store=c.store, api=hermes_api, telegram=fake_tg, now=clock.now,
    )
    assert out["action"] == "skip"
    (row,) = _sent_rows(c)
    first, second = (i["task_id"] for i in json.loads(row["items_json"]))  # numbering order of the message
    with hermes_api.connect("default") as conn:
        assert hermes_api.task(conn, first).status == "blocked"
        assert hermes_api.task(conn, second).status != "blocked"  # item 2 of the resend message


def test_a_failed_resend_is_retried_and_the_version_is_kept_until_it_lands(kanban_home, fake_tg, clock, tmp_path):
    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    fake_tg.fail_next = RuntimeError("network down")  # no HTTP status: transport failure
    c.evaluate(clock.now)
    assert _reminders(fake_tg) == [] and c.store.kv_get("template_version") != str(messages.MESSAGE_TEMPLATE_VERSION)
    clock.advance(31)
    c.evaluate(clock.now)
    assert _reminders(fake_tg) == []  # transport back-off
    clock.advance(200)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)


def test_waits_never_sent_are_not_resent_and_the_version_is_recorded(kanban_home, fake_tg, clock, tmp_path):
    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(60)  # nothing due yet, nothing ever sent: nothing is in the old format
    c.evaluate(clock.now)
    assert _texts(fake_tg) == []
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)


def test_a_wait_due_now_is_not_sent_twice_in_one_pass(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(30 * 60)
    _pretend_old_send(c, tids, evs, "reminder1", at=clock.now - 3600)  # old send, reminder2 not due yet
    clock.advance(2 * H)  # now reminder2 is due: the normal reminder IS the new format
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1
    assert c.store.latest_wait("default", tids[0])["stage"] == "reminder2"


# -- 2. first discovery of an old wait ------------------------------------------------------------
def test_old_wait_discovered_late_gets_a_message_now_and_the_next_digest_a_day_later(
    kanban_home, fake_tg, clock, tmp_path
):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(3 * D)  # blocked three days ago, the plugin sees it for the first time
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1  # not silently skipped, not waiting for a digest time
    assert c.store.latest_wait("default", tids[0])["stage"] == "digest"
    first = clock.now
    clock.advance(D - 600)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1  # not yet: a digest is a day after the previous message
    clock.now = first + D
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 2


def test_digest_repeats_a_period_after_the_last_send_in_the_tracker(kanban_home, clock, tmp_path):
    from test_waits import _tracker

    wt, store, _ = _tracker(tmp_path)
    with _api().connect("default") as conn:
        tid, ev = make_human_block(conn)
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(3 * D)
    (due,) = wt.collect_due(clock.now)
    assert due.stage == "digest"
    wt.mark_sent(due, clock.now)
    clock.advance(D - 1)
    assert wt.collect_due(clock.now) == []
    clock.advance(1)
    assert [d.stage for d in wt.collect_due(clock.now)] == ["digest"]


# -- 3. the end of the ladder without an escalation contact -----------------------------------------
def test_escalation_without_escalate_to_sends_one_setup_alert(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(8 * H)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1  # the owner reminder still goes out
    (alert,) = _setups(fake_tg)
    assert alert.startswith("[Kanban - Empowered]\n⚙️ KANBAN · SETUP\n") and "escalate_to" in alert
    assert "NEEDS YOU" not in alert  # a message of its own, never inside a reminder
    for _ in range(3):
        clock.advance(31)
        c.evaluate(clock.now)
    clock.advance(D)
    c.evaluate(clock.now)  # the daily digest comes, the alert does not
    assert len(_setups(fake_tg)) == 1


def test_no_setup_alert_before_the_escalation_or_with_a_contact(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(2 * H + 60)
    c.evaluate(clock.now)
    assert _setups(fake_tg) == []  # the escalation stage has not been reached
    c2 = _coord(tmp_path / "b", fake_tg, clock, escalate_to="ops")
    clock.advance(7 * H)
    c2.evaluate(clock.now)
    assert _setups(fake_tg) == []  # a contact is configured (and has a chat id)


def test_setup_alert_is_retried_after_a_failed_send(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(8 * H)
    orig = fake_tg.send
    failures = []

    class Rejected(Exception):
        status = 500

    def flaky(chat_id, text, **kw):
        if "KANBAN · SETUP" in text and not failures:
            failures.append(text)
            raise Rejected("boom")
        return orig(chat_id, text, **kw)

    fake_tg.send = flaky
    c.evaluate(clock.now)
    assert failures and _setups(fake_tg) == [] and len(_reminders(fake_tg)) == 1
    clock.advance(31)
    c.evaluate(clock.now)
    assert _setups(fake_tg) == []  # back-off between attempts
    clock.advance(700)
    c.evaluate(clock.now)
    assert len(_setups(fake_tg)) == 1
    clock.advance(700)
    c.evaluate(clock.now)
    assert len(_setups(fake_tg)) == 1  # once it landed, never again


# -- 4. waits.blocking_digest_every -----------------------------------------------------------------
def test_blocking_digest_is_a_setting_with_a_flat_form_key():
    from ke.settings import Settings

    s = Settings.from_mapping({})
    assert s.blocking_digest_s == s.digest_every_s == D  # default: same as digest
    assert Settings.from_mapping({"digest": "12h"}).blocking_digest_s == 12 * H  # follows a changed digest
    s = Settings.from_mapping({"waits": {"digest": "daily", "blocking_digest_every": "6h"}})
    assert s.blocking_digest_s == 6 * H and s.digest_every_s == D
    s = Settings.from_mapping({"blocking_digest_every": "hourly", "waits": {"blocking_digest_every": "6h"}})
    assert s.blocking_digest_s == H  # the form key wins, like the others
    assert Settings.from_mapping({"blocking_digest_every": "  "}).blocking_digest_s == D  # blank = default
    with pytest.raises(ValueError):
        Settings.from_mapping({"blocking_digest_every": "sometimes"})


def test_blocking_digest_read_from_ctx():
    from types import SimpleNamespace

    from ke.settings import Settings

    cfg = {"blocking_digest_every": "4h"}
    ctx = SimpleNamespace(get_config=lambda key, default=None: cfg.get(key, default))
    assert Settings.from_ctx(ctx).blocking_digest_s == 4 * H


def test_waits_that_hold_cards_use_the_blocking_cadence(kanban_home, clock, tmp_path):
    from test_waits import _tracker

    wt, store, _ = _tracker(tmp_path, blocking_digest_every_s=12 * H)
    with _api().connect("default") as conn:
        holder, ev1 = make_human_block(conn, title="holder")
        plain, ev2 = make_human_block(conn, title="plain")
        create_task(conn, title="child", parents=[holder])
    clock.now = float(max(ev1.created_at, ev2.created_at))
    wt.sync(clock.now)
    base = clock.now

    def stages(offset):
        clock.now = base + offset
        return {d.task_id: d.stage for d in wt.collect_due(clock.now)}

    for d in wt.collect_due(base + 8 * H):
        wt.mark_sent(d, base + 8 * H)  # both reach the escalation
    assert stages(8 * H + 12 * H - 60) == {}
    assert stages(8 * H + 12 * H) == {holder: "digest"}  # the holder: every 12 h
    wt.mark_sent(wt.collect_due(base + 20 * H)[0], base + 20 * H)
    assert stages(8 * H + D - 60) == {}
    assert stages(8 * H + D) == {holder: "digest", plain: "digest"}  # 32 h: the plain wait's first daily digest
    for d in wt.collect_due(base + 32 * H):
        wt.mark_sent(d, base + 32 * H)
    assert stages(32 * H + 12 * H) == {holder: "digest"}  # 12 h later only the holder again
    wt.mark_sent(wt.collect_due(base + 44 * H)[0], base + 44 * H)
    assert stages(32 * H + D - 60) == {}  # the plain wait keeps the daily cadence (its next one: 56 h)
    assert stages(32 * H + D) == {holder: "digest", plain: "digest"}


def test_blocking_cadence_equal_to_digest_changes_nothing(kanban_home, clock, tmp_path):
    from test_waits import _tracker

    wt, store, _ = _tracker(tmp_path)
    with _api().connect("default") as conn:
        holder, ev = make_human_block(conn)
        create_task(conn, title="child", parents=[holder])
    _start(clock, ev)
    wt.sync(clock.now)
    clock.advance(8 * H)
    for d in wt.collect_due(clock.now):
        wt.mark_sent(d, clock.now)
    clock.advance(D - 60)
    assert wt.collect_due(clock.now) == []
    clock.advance(60)
    assert [d.stage for d in wt.collect_due(clock.now)] == ["digest"]


# -- text: new catalog keys exist in every shipped catalog ------------------------------------------
def test_setup_alert_text_exists_in_every_catalog():
    import pathlib

    from agent.i18n_layers import parse_locale_file
    from ke.messages import EN

    root = pathlib.Path(__file__).resolve().parent.parent / "locales"
    assert "config.escalation_no_target" in EN
    for name in ("en", "pt", "pt-br"):
        assert "kanban_empowered.config.escalation_no_target" in parse_locale_file(root / f"{name}.yaml"), name


def test_setup_alert_in_portuguese(kanban_home, fake_tg, clock, tmp_path, pt_br):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(8 * H)
    c.evaluate(clock.now)
    (alert,) = _setups(fake_tg)
    assert "CONFIGURAÇÃO" in alert and "escalate_to" in alert


# -- fix round 1 ------------------------------------------------------------------------------------
def test_blank_nested_blocking_digest_means_same_as_digest():
    from ke.settings import Settings

    for blank in ("", "   ", None):
        s = Settings.from_mapping({"waits": {"digest": "12h", "blocking_digest_every": blank}})
        assert s.blocking_digest_every_s is None and s.blocking_digest_s == 12 * H


def test_one_wait_that_cannot_be_rebuilt_does_not_block_the_resend(kanban_home, fake_tg, clock, tmp_path, caplog):
    import logging

    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(2)
    _start(clock, evs[-1])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    bad = tids[0]
    orig = c.waits.due_for

    def due_for(row, now):
        if row["task_id"] == bad:
            raise RuntimeError("corrupt card")
        return orig(row, now)

    c.waits.due_for = due_for
    with caplog.at_level(logging.ERROR):
        c.evaluate(clock.now)
    (call,) = [m for m in fake_tg.calls["send"] if "NEEDS YOU" in m["text"]]
    (row,) = _sent_rows(c)
    assert [i["task_id"] for i in json.loads(row["items_json"])] == [tids[1]]
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)
    assert len([r for r in caplog.records if "could not rebuild" in r.getMessage()]) == 1
    clock.advance(31)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1
    assert len([r for r in caplog.records if "could not rebuild" in r.getMessage()]) == 1  # logged once


def test_skip_marker_stops_retrying_a_broken_wait_while_others_are_pending(kanban_home, fake_tg, clock, tmp_path, caplog):
    import logging

    from ke import messages

    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(2)
    _start(clock, evs[-1])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    orig = c.waits.due_for
    calls = []

    def due_for(row, now):
        calls.append(row["task_id"])
        if row["task_id"] == tids[0]:
            raise RuntimeError("corrupt card")
        return orig(row, now)

    c.waits.due_for = due_for
    fake_tg.fail_next = RuntimeError("network down")  # the first send fails: the version stays unwritten
    with caplog.at_level(logging.ERROR):
        c.evaluate(clock.now)
    assert c.store.kv_get("template_version") != str(messages.MESSAGE_TEMPLATE_VERSION)
    clock.advance(300)
    c.evaluate(clock.now)
    assert calls.count(tids[0]) == 1  # the broken one was skipped for good after its first failure
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)


def test_owner_without_chat_id_is_skipped_not_retried(kanban_home, fake_tg, clock, tmp_path):
    from ke import messages

    c = _coord(tmp_path, fake_tg, clock, owner_ids={"ops": 222})  # the default owner "sponsor" has no chat
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    c.evaluate(clock.now)
    assert _reminders(fake_tg) == []
    assert c.store.kv_get("template_version") == str(messages.MESSAGE_TEMPLATE_VERSION)  # not stuck on it


def test_due_reminder_and_resend_for_one_owner_are_one_message(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(2)
    _start(clock, evs[-1])
    clock.advance(30 * 60)
    # tids[0]: messaged long ago in the old layout, nothing due; tids[1]: its first reminder is due now
    c.store.upsert_wait("default", tids[0], evs[0].id, "sponsor", evs[0].created_at)
    c.store.mark_wait_sent("default", tids[0], evs[0].id, "reminder1", int(clock.now - 1))
    c.store.upsert_wait("default", tids[1], evs[1].id, "sponsor", evs[1].created_at)
    clock.advance(0)
    c.evaluate(clock.now)
    (text,) = _reminders(fake_tg)
    (row,) = _sent_rows(c)
    assert {i["task_id"] for i in json.loads(row["items_json"])} == set(tids)
    # the due one advanced its stage, the re-sent one did not
    assert c.store.latest_wait("default", tids[1])["stage"] == "reminder1"
    assert c.store.latest_wait("default", tids[0])["last_sent_at"] == int(clock.now - 1)
    clock.advance(31)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1


def test_markers_are_deleted_once_the_version_is_written(kanban_home, fake_tg, clock, tmp_path):
    c = _coord(tmp_path, fake_tg, clock)
    tids, evs = _blocks(1)
    _start(clock, evs[0])
    clock.advance(9 * H)
    _pretend_old_send(c, tids, evs, "escalation", at=clock.now - 60)
    c.evaluate(clock.now)
    assert len(_reminders(fake_tg)) == 1
    keys = [r["key"] for r in c.store.conn.execute("SELECT key FROM kv")]
    assert not [k for k in keys if k.startswith("tmpl")]


def test_kv_delete_prefix(tmp_path):
    from ke.store import Store

    st = Store(tmp_path / "s.db")
    for k in ("tmpl:1:a", "tmpl:1:b", "tmpl-skip:1:a", "other"):
        st.kv_set(k, "1")
    assert st.kv_delete_prefix("tmpl:") == 2
    assert st.kv_get("tmpl-skip:1:a") == "1" and st.kv_get("other") == "1" and st.kv_get("tmpl:1:a") is None


def test_explain_next_digest_follows_the_last_send_and_the_blocking_period(kanban_home, tmp_path):
    import time

    from ke import conditions, hermes_api as h
    from ke.settings import Settings
    from ke.store import Store

    store = Store(tmp_path / "state.db")
    with h.connect("default") as conn:
        tid, ev = make_human_block(conn)
        create_task(conn, title="child", parents=[tid])
        now = float(ev.created_at) + 3 * D
        store.upsert_wait("default", tid, ev.id, "sponsor", ev.created_at)
        store.mark_wait_sent("default", tid, ev.id, "digest", int(now - 2 * H))
        plain = Settings.from_mapping({})
        blocking = Settings.from_mapping({"blocking_digest_every": "6h"})
        text = lambda st: next(c for c in conditions.explain(conn, "default", tid, store, now, st).conditions
                               if c.name == "wait").text
        assert "next reminder in 22 h" in text(plain)  # a day after the last digest
        assert "next reminder in 4 h" in text(blocking)  # holds a card: 6 h after the last digest
