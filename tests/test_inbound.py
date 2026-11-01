"""Task 9: authenticated two-way Telegram replies and the ``done`` CLI fallback."""
from __future__ import annotations

import asyncio
import itertools
import types

import pytest

from conftest import block_task, create_task, settings_for_test
from test_waits import _last_blocked, make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

OWNER = "6802889854"  # sponsor: user id == DM chat id
BOSS = "5550001"  # escalate_to: a known identity, but not the owner of these waits
STRANGER = "999"

_ids = itertools.count(100)


# -- helpers -------------------------------------------------------------------
def _event(text, user_id, chat_id=OWNER, reply_to=None, message_id=None, platform="telegram",
           user_name="Dellanio", **source_kw):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    src = SessionSource(
        platform=Platform(platform), chat_id=str(chat_id), user_id=str(user_id), user_name=user_name,
        **source_kw,
    )
    return MessageEvent(
        text=text,
        source=src,
        message_id=str(message_id if message_id is not None else next(_ids)),
        reply_to_message_id=None if reply_to is None else str(reply_to),
        user_id=str(user_id),
        user_name=user_name,
    )


def reply_event(user_id, reply_to, text, **kw):
    return _event(text, user_id, reply_to=reply_to, **kw)


def plain_event(text, user_id=OWNER, **kw):
    return _event(text, user_id, **kw)


def _status(tid, board="default"):
    from ke import hermes_api

    with hermes_api.connect(board) as conn:
        return hermes_api.task(conn, tid).status


def _comments(tid):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        return [c.body for c in hermes_api.comments(conn, tid)]


def _reblock(tid):
    """Unblock and block ``tid`` again: a new block (new version) of the same card.

    Untyped (still a human wait) because a same-kind re-block is routed to triage by Hermes.
    """
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        if hermes_api.task(conn, tid).status == "blocked":
            assert hermes_api.unblock(conn, tid)
        assert block_task(conn, tid, reason="Preciso: de novo\nPor quê: x\nComo responder: y", kind=None)
        assert hermes_api.task(conn, tid).status == "blocked"


class Env(types.SimpleNamespace):
    def handle(self, ev):
        from ke.inbound import handle_inbound

        return handle_inbound(ev, **self.kw)

    @property
    def kw(self):
        return dict(settings=self.settings, store=self.store, api=self.api, telegram=self.telegram,
                    now=self.clock.now)


def _env(tmp_path, fake_tg, clock, n_items=1, **overrides):
    from ke import hermes_api
    from ke.store import Store
    from ke.waits import WaitTracker

    overrides.setdefault("owner_ids", {"sponsor": int(OWNER), "boss": int(BOSS)})
    overrides.setdefault("escalate_to", "boss")
    settings = settings_for_test(tmp_path, **overrides)
    store = Store(settings.shared_state_dir / "state.db")
    items, tids = [], []
    with hermes_api.connect("default") as conn:
        for i in range(n_items):
            tid, ev = make_human_block(conn, title=f"card {i}")
            tids.append(tid)
            items.append({"board": "default", "task_id": tid, "version": int(ev.id)})
        held = create_task(conn, title="held", parents=[tids[0]])
    clock.now = float(ev.created_at) + 1800
    WaitTracker(settings, store, hermes_api).sync(clock.now)
    sent_id = 4242
    store.add_sent_message(sent_id, int(OWNER), "waits", items, int(clock.now))
    return Env(settings=settings, store=store, api=hermes_api, telegram=fake_tg, clock=clock,
               tids=tids, items=items, held=held, sent_id=sent_id)


@pytest.fixture
def env(kanban_home, tmp_path, fake_tg, clock):
    return _env(tmp_path, fake_tg, clock)


@pytest.fixture
def env2(kanban_home, tmp_path, fake_tg, clock):
    return _env(tmp_path, fake_tg, clock, n_items=2)


class SpyApi:
    """Wraps ``hermes_api`` and records every call, to prove no card was read."""

    def __init__(self):
        from ke import hermes_api

        self._api = hermes_api
        self.calls = []

    def __getattr__(self, name):
        fn = getattr(self._api, name)

        def wrapper(*a, **kw):
            self.calls.append(name)
            return fn(*a, **kw)

        return wrapper


# -- brief tests ---------------------------------------------------------------
def test_non_owner_reply_is_ignored(env):
    ev = reply_event(user_id=STRANGER, reply_to=env.sent_id, text="feito")
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


def test_replay_is_consumed_once(env):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito", message_id="77")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) != "blocked"
    sends = env.telegram.count("send")
    _reblock(env.tids[0])
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert env.telegram.count("send") == sends  # a replay is consumed in silence


def test_stale_version_does_not_unblock(env):
    _reblock(env.tids[0])  # re-blocked after the reminder went out; the tracker has NOT synced yet
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert "changed" in env.telegram.last_text


def test_stale_version_after_tracker_sync_does_not_unblock(env):
    from ke.waits import WaitTracker

    _reblock(env.tids[0])
    WaitTracker(env.settings, env.store, env.api).sync(env.clock.now)
    env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
    assert _status(env.tids[0]) == "blocked" and "changed" in env.telegram.last_text


def test_stale_version_in_pt_br(env, pt_br):  # noqa: F811
    _reblock(env.tids[0])
    env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito"))
    assert _status(env.tids[0]) == "blocked" and "mudou" in env.telegram.last_text


def test_bare_done_on_grouped_message_asks_which(env2):
    assert env2.handle(reply_event(user_id=OWNER, reply_to=env2.sent_id, text="feito"))["action"] == "skip"
    assert [_status(t) for t in env2.tids] == ["blocked", "blocked"]
    assert "which item" in env2.telegram.last_text.lower()


def test_bare_done_on_grouped_message_asks_which_in_pt_br(env2, pt_br):  # noqa: F811
    env2.handle(reply_event(user_id=OWNER, reply_to=env2.sent_id, text="feito"))
    assert [_status(t) for t in env2.tids] == ["blocked", "blocked"]
    assert "qual item" in env2.telegram.last_text.lower()


def test_numbered_done_unblocks_only_that_item(env2):
    assert env2.handle(reply_event(user_id=OWNER, reply_to=env2.sent_id, text="2 feito"))["action"] == "skip"
    assert _status(env2.tids[0]) == "blocked" and _status(env2.tids[1]) != "blocked"


def test_unrelated_message_passes_through(env):
    assert env.handle(plain_event(text="oi")) is None
    assert env.telegram.count("send") == 0


# -- authentication ------------------------------------------------------------
def test_unknown_stranger_gets_no_reply_and_no_card_read(env):
    spy = SpyApi()
    env.api = spy
    ev = reply_event(user_id=STRANGER, reply_to=env.sent_id, text="feito", message_id="5")
    assert env.handle(ev) is None
    assert spy.calls == [] and env.telegram.count("send") == 0
    # nothing was consumed either: the update key is still free
    assert env.store.mark_update(f"tg:{OWNER}:5", int(env.clock.now)) is True


def test_known_non_owner_is_ignored(env):
    spy = SpyApi()
    env.api = spy
    ev = reply_event(user_id=BOSS, reply_to=env.sent_id, text="feito", message_id="41")
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"
    assert spy.calls == [] and env.telegram.count("send") == 0
    assert env.store.mark_update(f"tg:{OWNER}:41", int(env.clock.now)) is True  # not consumed


def test_copy_recipient_cannot_resolve(env):
    copy_id = 9001
    env.store.add_sent_message(copy_id, int(BOSS), "waits", env.items, int(env.clock.now))
    ev = reply_event(user_id=BOSS, chat_id=BOSS, reply_to=copy_id, text="feito", message_id="42")
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"
    assert env.telegram.count("send") == 0
    assert env.store.mark_update(f"tg:{BOSS}:42", int(env.clock.now)) is True  # not consumed


def test_owner_impersonation_in_another_chat_is_ignored(env):
    # same message id, but in a chat where the plugin never sent it
    ev = reply_event(user_id=OWNER, chat_id=BOSS, reply_to=env.sent_id, text="feito")
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


def test_non_telegram_platform_passes_through(env):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito", platform="discord")
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


@pytest.mark.parametrize("flag", ["is_bot", "delivered_via_upstream_relay"])
def test_bot_and_relayed_events_pass_through(env, flag):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito", **{flag: True})
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


def test_internal_event_passes_through(env):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev.internal = True
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


def test_missing_message_id_passes_through(env):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev.message_id = None
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"


def _raw_reply(author_id):
    return types.SimpleNamespace(reply_to_message=types.SimpleNamespace(from_user=types.SimpleNamespace(id=author_id)))


def test_reply_to_another_bots_message_with_same_id_passes_through(env):
    env.telegram.bot_id = 111
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev.raw_message = _raw_reply(222)  # DM message ids are per bot: this is not our reminder
    assert env.handle(ev) is None
    assert _status(env.tids[0]) == "blocked"
    ev2 = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev2.raw_message = _raw_reply(111)
    assert env.handle(ev2)["action"] == "skip"
    assert _status(env.tids[0]) != "blocked"


def test_telegram_client_bot_id_comes_from_the_token():
    from ke.telegram import TelegramClient

    assert TelegramClient("123456:ABC-def").bot_id == 123456
    assert TelegramClient("garbage").bot_id is None


def test_reply_to_unknown_message_passes_through(env):
    assert env.handle(reply_event(user_id=OWNER, reply_to=1, text="feito")) is None
    assert _status(env.tids[0]) == "blocked"


def test_owner_free_text_reply_is_the_answer(env):
    # message format v2 (spec section 5e, supersedes Ruling 10): the owner's free text answering a
    # single-item reminder is the answer: the card is unblocked and the text becomes a comment
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="what is this about?", message_id="6")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) != "blocked"
    assert "what is this about?" in _comments(env.tids[0])[-1]
    assert env.store.mark_update(f"tg:{OWNER}:6", int(env.clock.now)) is False  # consumed


# -- request validity ----------------------------------------------------------
def test_expired_request_is_refused(env):
    env.clock.advance(env.settings.reply_ttl_s)
    assert env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito"))["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert "expired" in env.telegram.last_text


def test_out_of_range_number_asks_which(env2):
    for text in ("3 done", "0 done"):
        assert env2.handle(reply_event(user_id=OWNER, reply_to=env2.sent_id, text=text))["action"] == "skip"
        assert "which item" in env2.telegram.last_text.lower()
    assert [_status(t) for t in env2.tids] == ["blocked", "blocked"]


def test_single_item_message_with_another_leading_number_is_plain_text(env):
    # fix round 1, ruling 11: on a one-item reminder "2 done" is the answer text, not item 2
    env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="2 done"))
    assert _status(env.tids[0]) != "blocked" and "2 done" in _comments(env.tids[0])[-1]


def test_card_no_longer_blocked_is_reported_as_changed(env):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        assert hermes_api.unblock(conn, env.tids[0])
    env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
    assert "changed" in env.telegram.last_text
    assert not any(a["kind"] == "human_done" for a in env.store.actions_since(0))


@pytest.mark.parametrize(
    "text",
    ["done", "Feito", "liguei", "OK", "approved", "aprovado", "  1 done", "DONE!", "1. feito", "1) feito",
     "1 done!", "feito ✅", "ok 👍", "1 - ok", "1: done."],
)
def test_explicit_confirmations_unblock(env, text):
    assert env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text=text))["action"] == "skip"
    assert _status(env.tids[0]) != "blocked"


@pytest.mark.parametrize(
    "text",
    ["doneish", "okay", "feitos", "1 2 done", "ok, mas ainda nao", "feito?",
     "liguei mas ninguem atendeu", "done yet?", "OK vou ver amanha", "aprovado so a parte A", "done-ish",
     "1 feito, pode seguir", "feito, liguei a placa"],
)
def test_any_other_owner_text_is_the_answer(env, text):
    # message format v2 (spec section 5e): free text from the owner resolves the item and is commented
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text=text, message_id="61")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) != "blocked"
    from ke.inbound import split_reply

    # the comment is the text without its message-local item number (fix round 1)
    assert _comments(env.tids[0])[-1].endswith(": " + " ".join(split_reply(text)[1].split()))


@pytest.mark.parametrize("text", ["não feito", "ainda não", "1 — nao", "negativo", "not yet", "nope", "no."])
def test_an_explicit_not_done_keeps_the_card_waiting(env, text):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text=text, message_id="62")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert "still waiting" in _comments(env.tids[0])[-1]


@pytest.mark.parametrize("text", ["2 done!", "2. feito", "2) feito"])
def test_numbered_confirmation_forms_pick_the_item(env2, text):
    assert env2.handle(reply_event(user_id=OWNER, reply_to=env2.sent_id, text=text))["action"] == "skip"
    assert _status(env2.tids[0]) == "blocked" and _status(env2.tids[1]) != "blocked"


def test_gateway_control_disallowed_or_prompt_answer_passes_through(env):
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev.allow_gateway_control = False
    assert env.handle(ev) is None
    ev2 = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    ev2.prompt_response = {"prompt_id": "p", "option_id": "o"}
    assert env.handle(ev2) is None
    assert _status(env.tids[0]) == "blocked" and env.telegram.count("send") == 0


def test_failure_after_consuming_still_skips(env, monkeypatch):
    from ke import inbound

    def boom(*a, **kw):
        raise RuntimeError("render broke")

    monkeypatch.setattr(inbound, "render_answer", boom)
    r = env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
    assert r == {"action": "skip", "reason": "kanban-empowered"}
    assert _status(env.tids[0]) != "blocked"


def test_unchecked_bot_author_is_logged_once(env, monkeypatch, caplog):
    from ke import inbound

    monkeypatch.setattr(inbound, "_logged_once", set())
    with caplog.at_level("WARNING", logger="ke.inbound"):
        env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
        _reblock(env.tids[0])
        env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
    assert sum("cannot verify" in r.getMessage() for r in caplog.records) == 1


# -- the action ----------------------------------------------------------------
def test_owner_done_unblocks_comments_logs_and_confirms(env):
    r = env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="Feito ✅", message_id="31"))
    assert r == {"action": "skip", "reason": "kanban-empowered"}
    tid = env.tids[0]
    assert _status(tid) != "blocked"
    body = _comments(tid)[-1]
    assert body.startswith("kanban-empowered: ")
    assert "Telegram" in body and "Dellanio (6802889854)" in body and "Feito ✅" in body
    actions = [a for a in env.store.actions_since(0) if a["kind"] == "human_done"]
    assert [(a["board"], a["task_id"]) for a in actions] == [("default", tid)]
    send = env.telegram.calls["send"][-1]
    assert send["chat_id"] == int(OWNER) and send.get("reply_to") == 31
    # the board and the action, no ids (message format v2)
    assert send["text"].startswith("[Kanban - Empowered]\n✅ DEFAULT: a") and "1 card" in send["text"]
    assert tid not in send["text"] and env.held not in send["text"]


def test_internal_error_after_consuming_answers_and_skips(env, monkeypatch):
    def boom(conn, tid):
        raise RuntimeError("db locked")

    spy = SpyApi()
    monkeypatch.setattr(spy, "unblock", boom, raising=False)
    env.api = spy
    r = env.handle(reply_event(user_id=OWNER, reply_to=env.sent_id, text="done"))
    assert r["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert env.telegram.count("send") == 1  # the owner hears it failed


# -- the hook wrapper ----------------------------------------------------------
def _hook(env, store=None):
    from ke.inbound import make_dispatch_hook

    return make_dispatch_hook(env.settings, store or env.store, env.api, env.telegram, clock=env.clock)


def test_hook_consumes_owner_done(env):
    cb = _hook(env)
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="done")
    r = asyncio.run(cb(event=ev, gateway=None, session_store=None))
    assert r["action"] == "skip" and _status(env.tids[0]) != "blocked"


def test_hook_filters_non_candidates_on_the_loop(env, monkeypatch):
    from ke import inbound

    hops = []

    async def fake_to_thread(fn, *a, **kw):
        hops.append(fn)
        return fn(*a, **kw)

    monkeypatch.setattr(inbound.asyncio, "to_thread", fake_to_thread)
    cb = _hook(env)
    for ev in (plain_event("oi"), reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito", platform="discord")):
        assert asyncio.run(cb(event=ev, gateway=None, session_store=None)) is None
    assert hops == []
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="feito")
    assert asyncio.run(cb(event=ev, gateway=None, session_store=None))["action"] == "skip"
    assert len(hops) == 1


def test_hook_internal_error_passes_through(env):
    class BrokenStore:
        def __getattr__(self, name):
            raise RuntimeError("store is gone")

    cb = _hook(env, store=BrokenStore())
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="done")
    assert asyncio.run(cb(event=ev, gateway=None, session_store=None)) is None
    assert asyncio.run(cb(event=None)) is None
    assert _status(env.tids[0]) == "blocked"


def test_register_wires_dispatch_hook_only_in_coordinator(kanban_home, tmp_path, load_plugin):
    from test_doctor import _Ctx

    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)
    assert "pre_gateway_dispatch" in ctx.hooks
    assert asyncio.run(ctx.hooks["pre_gateway_dispatch"](event=plain_event("oi"), gateway=None, session_store=None)) is None

    mod2 = load_plugin("ke_plugin_pkg2")
    ctx2 = _Ctx(tmp_path / "state2", config={"coordinator_profile": "other"})
    mod2.register(ctx2)
    assert "pre_gateway_dispatch" not in ctx2.hooks


# -- cli done ------------------------------------------------------------------
def _cli_done(env, card, board=None):
    from ke import cli

    return cli.run_done(env.settings, card, board)


def test_cli_done_unblocks_with_os_user(env, capsys, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "della")
    tid = env.tids[0]
    assert _cli_done(env, tid) == 0
    assert _status(tid) != "blocked"
    body = _comments(tid)[-1]
    assert body.startswith("kanban-empowered: ") and "CLI" in body and "cli:della" in body
    assert [a["task_id"] for a in env.store.actions_since(0) if a["kind"] == "human_done"] == [tid]
    out = capsys.readouterr().out
    assert tid in out and env.held in out


def test_cli_done_refuses_a_changed_block(env, capsys):
    tid = env.tids[0]
    _reblock(tid)  # the tracked wait is the old version
    assert _cli_done(env, tid, "default") == 1
    assert _status(tid) == "blocked"
    assert "changed" in capsys.readouterr().out


def test_cli_done_refuses_a_card_that_is_not_blocked(env, capsys):
    from ke import hermes_api

    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="free", assignee="dev")
    assert _cli_done(env, tid) == 1
    assert not any(a["kind"] == "human_done" for a in env.store.actions_since(0))


def test_cli_done_unknown_card(env, capsys):
    assert _cli_done(env, "t_nope") == 1
    assert "not found" in capsys.readouterr().out


def test_cli_done_is_wired_in_the_parser(env, monkeypatch):
    import argparse

    from ke import cli

    seen = {}
    monkeypatch.setattr(cli, "run_done", lambda s, card, board: seen.update(card=card, board=board) or 0)
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.run(parser.parse_args(["done", "t_1", "--board", "b"])) == 0
    assert seen == {"card": "t_1", "board": "b"}


# -- a breaker re-park is a new block (final review I-1) ------------------------------
@pytest.mark.parametrize("synced", [False, True])
def test_done_to_an_old_reminder_cannot_resolve_a_breaker_repark(env, synced):
    from ke.waits import WaitTracker
    from test_waits import breaker_park

    breaker_park(env.tids[0])  # unblocked, then parked by the failure breaker (block_kind survives)
    if synced:
        WaitTracker(env.settings, env.store, env.api).sync(env.clock.now)
    ev = reply_event(user_id=OWNER, reply_to=env.sent_id, text="done")
    assert env.handle(ev)["action"] == "skip"
    assert _status(env.tids[0]) == "blocked"
    assert "changed" in env.telegram.last_text
