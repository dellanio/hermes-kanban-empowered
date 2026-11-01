import io
import json
import logging
import urllib.error

import pytest

from conftest import make_coordinator
from test_waits import _start, make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

H = 3600


# -- client --------------------------------------------------------------------
class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def fake_opener(calls, body=None, exc=None):
    def opener(req, timeout=None):
        calls.append(req)
        req.timeout_seen = timeout
        if exc is not None:
            raise exc
        return _Resp(body)

    return opener


def _client(calls, body=None, exc=None, token="T"):
    from ke.telegram import TelegramClient

    return TelegramClient(token, opener=fake_opener(calls, body, exc))


def test_send_posts_json_and_returns_id():
    calls = []
    c = _client(calls, {"ok": True, "result": {"message_id": 42}})
    assert c.send(6802889854, "oi") == 42
    req = calls[0]
    assert req.full_url == "https://api.telegram.org/botT/sendMessage"
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"chat_id": 6802889854, "text": "oi"}
    assert req.timeout_seen == 10


def test_send_reply_to_and_custom_base_and_timeout():
    from ke.telegram import TelegramClient

    calls = []
    c = TelegramClient(
        "T", base="http://x.test/", timeout_s=3,
        opener=fake_opener(calls, {"ok": True, "result": {"message_id": 1}}),
    )
    c.send(5, "a", reply_to=9)
    assert calls[0].full_url == "http://x.test/botT/sendMessage"
    assert json.loads(calls[0].data)["reply_parameters"]["message_id"] == 9
    assert calls[0].timeout_seen == 3


def test_edit_and_pin_payloads():
    calls = []
    c = _client(calls, {"ok": True, "result": True})
    assert c.edit(5, 77, "novo") is None
    assert c.pin(5, 77) is None
    assert calls[0].full_url.endswith("/botT/editMessageText")
    assert json.loads(calls[0].data) == {"chat_id": 5, "message_id": 77, "text": "novo"}
    assert calls[1].full_url.endswith("/botT/pinChatMessage")
    assert json.loads(calls[1].data) == {"chat_id": 5, "message_id": 77, "disable_notification": True}


def test_api_error_raises():
    from ke.telegram import TelegramError

    c = _client([], {"ok": False, "error_code": 429, "description": "Too Many Requests"}, token="123:abc")
    with pytest.raises(TelegramError) as e:
        c.send(1, "x")
    assert e.value.status == 429 and e.value.description == "Too Many Requests"


def test_http_error_body_is_parsed():
    from ke.telegram import TelegramError

    body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"})
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body.encode()))
    with pytest.raises(TelegramError) as e:
        _client([], exc=err).edit(1, 2, "x")
    assert e.value.status == 400 and "message to edit not found" in e.value.description


def test_transport_errors_become_telegram_error_without_the_token():
    from ke.telegram import TelegramError

    for exc in (urllib.error.URLError("dns failed for botSECRET"), TimeoutError("timed out SECRET")):
        with pytest.raises(TelegramError) as e:
            _client([], exc=exc, token="SECRET").send(1, "x")
        assert e.value.status is None
        assert "SECRET" not in str(e.value) and "SECRET" not in e.value.description


def test_non_numeric_error_code_only_raises_telegram_error():
    from ke.telegram import TelegramError

    for code in ("abc", None, [1], {}):
        with pytest.raises(TelegramError) as e:
            _client([], {"ok": False, "error_code": code, "description": "nope"}).send(1, "x")
        assert e.value.status is None and e.value.description == "nope"


def test_http_error_is_closed_after_reading():
    from ke.telegram import TelegramError

    fp = io.BytesIO(json.dumps({"ok": False, "description": "boom"}).encode())
    err = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, fp)
    with pytest.raises(TelegramError) as e:
        _client([], exc=err).send(1, "x")
    assert e.value.status == 502 and e.value.description == "boom" and fp.closed


def test_garbage_and_missing_message_id_raise():
    from ke.telegram import TelegramError

    with pytest.raises(TelegramError):
        _client([], b"<html>").send(1, "x")
    with pytest.raises(TelegramError):
        _client([], {"ok": True, "result": {}}).send(1, "x")


def test_client_never_retries():
    calls = []
    c = _client(calls, exc=urllib.error.URLError("down"))
    with pytest.raises(Exception):
        c.send(1, "x")
    assert len(calls) == 1


# -- message splitting ----------------------------------------------------------
def _due(task_id="t_1", board="b", owner="sponsor", stage="reminder1", need=None, why="w", age_s=1800, held=(),
         options=()):
    """A due whose action names its (short, non-hex) task id, so a message shows which item is which."""
    from ke.asks import Ask
    from ke.waits import Due

    need = f"do {task_id}" if need is None else need
    return Due(board, task_id, 1, owner, stage, age_s, Ask(need, why, "r", tuple(options)), list(held), "")


# a big item: message format v2 clips the request and the context, so bulk comes from options
FAT = tuple((chr(65 + k), "x" * 100) for k in range(8))


def _blocks(text):
    """The item blocks of a reminder (split at the separator line)."""
    from ke.messages import SEPARATOR

    return text.split(SEPARATOR)


def test_small_batch_is_one_message_with_items_in_order():
    from ke.messages import render_waits_chunks

    dues = [_due("t_a"), _due("t_b"), _due("t_c")]
    ((text, items),) = render_waits_chunks("sponsor", dues)
    blocks = _blocks(text)
    assert items == dues and len(blocks) == 3
    assert "① B" in blocks[0] and "do t_a" in blocks[0] and "③ B" in blocks[2] and "do t_c" in blocks[2]


def _big(i):
    # message format v2 clips the request and the context, so a big item is one with many options
    return _due(f"t_{i:02d}", options=[(chr(65 + k), f"{'x' * 100} {k}") for k in range(8)])


def test_oversized_batch_splits_at_item_boundaries_and_numbering_restarts():
    from ke.messages import _tg_len, circled, render_waits_chunks

    dues = [_big(i) for i in range(10)]
    chunks = render_waits_chunks("sponsor", dues)
    assert len(chunks) > 1
    assert [d for _, items in chunks for d in items] == dues  # nothing lost, nothing repeated, order kept
    for text, items in chunks:
        assert _tg_len(text) <= 4096
        assert text.startswith("[Kanban - Empowered]\n📋 KANBAN · NEEDS YOU\n")  # brand, then the header in every chunk
        if len(items) > 1:  # "n — ..." in this message maps to items[n-1] of this message only
            for n, (due, block) in enumerate(zip(items, _blocks(text)), start=1):
                assert f"{circled(n)} B" in block and f"do {due.task_id}" in block
        else:
            assert f"do {items[0].task_id}" in text
        for k in range(8):
            assert f"{chr(65 + k)} — {'x' * 100} {k}" in text  # no item is cut when it fits


def test_single_oversized_item_is_clipped_keeping_head_and_last_line():
    from ke.messages import _tg_len, render_waits_chunks

    dues = [_big(0), _due("t_ok")]
    chunks = render_waits_chunks("sponsor", dues, limit=600)
    assert [[d.task_id for d in items] for _, items in chunks] == [["t_00"], ["t_ok"]]
    text = chunks[0][0]
    assert _tg_len(text) <= 600 and "…" in text
    assert text.splitlines()[:2] == ["[Kanban - Empowered]", "📋 KANBAN · NEEDS YOU"] and text.splitlines()[-1] == "w"


def test_clip_of_a_huge_ask_is_fast_and_within_the_limit():
    import time

    from ke.messages import _tg_len, render_waits_chunks

    due = _due("t_big", need="é" * 60000)
    start = time.perf_counter()
    ((text, _),) = render_waits_chunks("sponsor", [due])
    assert time.perf_counter() - start < 0.05
    assert _tg_len(text) <= 4096 and "…" in text and text.splitlines()[-1] == "w"


def test_pathological_clip_counts_utf16_units():
    from ke.messages import _clip_item, _tg_len

    out = _clip_item("😀" * 60 + "\n" + "😀" * 60, 11)
    assert _tg_len(out) <= 11 and out.endswith("…")


# -- coordinator delivery -------------------------------------------------------
OWNERS = {"sponsor": 111, "ops": 222}


def _coord(tmp_path, fake_tg, clock=None, **kw):
    kw.setdefault("owner_ids", dict(OWNERS))
    return make_coordinator(tmp_path, clock=clock, telegram=fake_tg, **kw)


def _sent_rows(coord):
    return [dict(r) for r in coord.store.conn.execute("SELECT * FROM sent_messages ORDER BY message_id")]


def _real_wait(coord, clock, **block_kw):
    from ke import hermes_api

    # the real Hermes here has no veto hooks: mute that warning, it is not what these tests are about
    coord.store.kv_set("veto_hooks_warned", "1")
    coord.store.kv_set("veto_hooks_warn_sent", "1")
    with hermes_api.connect("default") as conn:
        tid, ev = make_human_block(conn, **block_kw)
    _start(clock, ev)
    coord.evaluate(clock.now)  # sync only
    return tid, ev


def test_failed_send_does_not_mark_sent(kanban_home, clock, fake_tg, tmp_path):
    from ke import hermes_api
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, api=hermes_api)
    tid, ev = _real_wait(coord, clock)
    clock.advance(1800)
    fake_tg.fail_next = TelegramError(500, "x")
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 0
    assert coord.store.latest_wait("default", tid)["stage"] is None
    assert [d.task_id for d in coord.pending_dues] == [tid]
    assert _sent_rows(coord) == []
    clock.advance(30)
    coord.evaluate(clock.now)  # retried on the next evaluation
    assert fake_tg.count("send") == 1
    assert coord.store.latest_wait("default", tid)["stage"] == "reminder1"
    assert coord.pending_dues == []


def test_non_telegram_exception_is_contained_too(kanban_home, clock, fake_tg, tmp_path):
    from ke import hermes_api

    coord = _coord(tmp_path, fake_tg, clock, api=hermes_api)
    tid, _ = _real_wait(coord, clock)
    clock.advance(1800)
    fake_tg.fail_next = OSError("network is down")
    coord.evaluate(clock.now)
    assert coord.store.latest_wait("default", tid)["stage"] is None


def test_successful_send_records_message_and_marks_sent_once(kanban_home, clock, fake_tg, tmp_path):
    from ke import hermes_api

    coord = _coord(tmp_path, fake_tg, clock, api=hermes_api)
    tid, ev = _real_wait(coord, clock)
    clock.advance(1800)
    coord.evaluate(clock.now)
    (call,) = fake_tg.calls["send"]
    assert call["chat_id"] == 111 and "👉 a" in call["text"] and tid not in call["text"]
    (row,) = _sent_rows(coord)
    assert (row["message_id"], row["chat_id"], row["kind"]) == (call["message_id"], 111, "waits")
    assert json.loads(row["items_json"]) == [{"board": "default", "task_id": tid, "version": ev.id}]
    assert row["created_at"] == int(clock.now)
    assert coord.store.latest_wait("default", tid)["stage"] == "reminder1"
    clock.advance(30)
    coord.evaluate(clock.now)
    assert fake_tg.count("send") == 1


def test_dues_are_grouped_one_message_per_owner(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    coord.pending_dues = [
        _due("t_1", owner="sponsor"), _due("t_2", owner="ops"), _due("t_3", owner="sponsor"),
    ]
    coord._deliver_waits(clock.now)
    by_chat = {c["chat_id"]: c for c in fake_tg.calls["send"]}
    assert set(by_chat) == {111, 222} and fake_tg.count("send") == 2
    blocks = _blocks(by_chat[111]["text"])
    assert "① B" in blocks[0] and "do t_1" in blocks[0] and "② B" in blocks[1] and "do t_3" in blocks[1]
    assert "do t_2" in by_chat[222]["text"] and "do t_1" not in by_chat[222]["text"]
    rows = {r["chat_id"]: json.loads(r["items_json"]) for r in _sent_rows(coord)}
    assert [i["task_id"] for i in rows[111]] == ["t_1", "t_3"]  # same order as the numbering
    assert coord.pending_dues == []


def _track(coord, fake_tg, fail_chats=None):
    """Count every send attempt; ``fail_chats`` maps chat_id -> exception raised while it is set."""
    attempts = []
    fail_chats = {} if fail_chats is None else fail_chats
    orig = fake_tg.send

    def send(chat_id, text, **kw):
        attempts.append(chat_id)
        exc = fail_chats.get(chat_id)
        if exc is not None:
            raise exc
        return orig(chat_id, text, **kw)

    fake_tg.send = send
    return attempts, fail_chats


def _wait_row(coord, due, owner=None):
    coord.store.upsert_wait(due.board, due.task_id, due.version, owner or due.owner, 0)


def _stage(coord, due):
    return coord.store.latest_wait(due.board, due.task_id)["stage"]


def _no_urgent(text):
    return "urgent" not in text.lower() and "🚨" not in text


def test_escalation_goes_to_owner_and_escalate_to(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [_due("t_1", stage="escalation", age_s=8 * H)]
    coord._deliver_waits(clock.now)
    assert sorted(c["chat_id"] for c in fake_tg.calls["send"]) == [111, 222]
    assert all(_no_urgent(c["text"]) for c in fake_tg.calls["send"])
    assert {r["chat_id"] for r in _sent_rows(coord)} == {111, 222}


def test_escalated_copy_has_a_follow_up_lead_and_no_role_name(tmp_path, fake_tg, clock):
    from ke.messages import t

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [_due("t_1", stage="escalation"), _due("t_2", stage="escalation")]
    coord._deliver_waits(clock.now)
    copy = next(c for c in fake_tg.calls["send"] if c["chat_id"] == 222)
    owner_msg = next(c for c in fake_tg.calls["send"] if c["chat_id"] == 111)
    assert copy["text"].splitlines()[:2] == ["[Kanban - Empowered]", t("waits.copy.lead")]
    assert "sponsor" not in copy["text"] and "copy" not in owner_msg["text"].lower()
    blocks = _blocks(copy["text"])
    assert "① B" in blocks[0] and "do t_1" in blocks[0] and "② B" in blocks[1] and "do t_2" in blocks[1]
    assert "↩️" not in copy["text"]  # the copy cannot be answered


def test_escalation_without_escalate_to_is_owner_only_and_not_marked_urgent(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    coord.pending_dues = [_due("t_1", stage="escalation", age_s=8 * H)]
    coord._deliver_waits(clock.now)
    (call,) = fake_tg.calls["send"]
    assert call["chat_id"] == 111 and call["text"].startswith("[Kanban - Empowered]\n📋 KANBAN · NEEDS YOU") and _no_urgent(call["text"])


def test_escalate_to_same_as_owner_sends_once(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="sponsor")
    coord.pending_dues = [_due("t_1", stage="escalation")]
    coord._deliver_waits(clock.now)
    (call,) = fake_tg.calls["send"]
    assert call["chat_id"] == 111 and _no_urgent(call["text"])


def test_mixed_batch_escalations_and_copies_per_owner(tmp_path, fake_tg, clock):
    from ke.messages import t

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [
        _due("t_s", owner="sponsor", stage="escalation"),
        _due("t_o", owner="ops", stage="escalation"),
    ]
    coord._deliver_waits(clock.now)
    by_chat = {}
    for c in fake_tg.calls["send"]:
        by_chat.setdefault(c["chat_id"], []).append(c["text"])
    assert all(_no_urgent(m) for msgs in by_chat.values() for m in msgs)
    assert any("do t_o" in m and not m.startswith("[Kanban - Empowered]\n" + t("waits.copy.lead")) for m in by_chat[222])  # ops' own
    assert any("do t_s" in m and m.startswith("[Kanban - Empowered]\n" + t("waits.copy.lead")) for m in by_chat[222])  # the copy
    assert not any("do t_o" in m for m in by_chat[111])


def test_non_escalation_stages_are_not_copied(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [_due("t_1", stage="reminder2"), _due("t_2", stage="digest")]
    coord._deliver_waits(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [111]


def test_escalation_copy_is_retried_until_it_lands_without_resending_to_the_owner(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    due = _due("t_1", stage="escalation")
    _wait_row(coord, due)
    attempts, fail = _track(coord, fake_tg, {222: TelegramError(500, "down")})
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222]
    assert _stage(coord, due) is None and coord.pending_dues == [due]  # not done: the copy is missing
    clock.advance(601)  # past the copy back-off
    coord.pending_dues = [due]  # the tracker offers the still-unsent wait again
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222, 222]  # only the copy again
    del fail[222]
    clock.advance(601)
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222, 222, 222]
    assert _stage(coord, due) == "escalation" and coord.pending_dues == []
    coord.pending_dues = [due]  # even if offered once more nothing is re-sent
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222, 222, 222]


def test_rate_limited_copy_backs_off_per_wait(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    due, other = _due("t_1", stage="escalation"), _due("t_2", stage="escalation")
    for d in (due, other):
        _wait_row(coord, d)
    attempts, fail = _track(coord, fake_tg, {222: TelegramError(429, "Too Many Requests")})
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222]
    for dt in (30, 300, 599):  # no new attempt while backing off
        coord.pending_dues = [due]
        coord._deliver_waits(clock.now + dt)
    assert attempts == [111, 222]
    coord.pending_dues = [due, other]  # a different wait is not held back by this one's back-off
    coord._deliver_waits(clock.now + 100)
    assert attempts == [111, 222, 111, 222]
    del fail[222]
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now + 600)
    assert attempts[-1] == 222 and _stage(coord, due) == "escalation"


def test_permanent_copy_rejection_gives_up_after_one_attempt(tmp_path, fake_tg, clock, caplog):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    due = _due("t_1", stage="escalation")
    _wait_row(coord, due)
    attempts, _ = _track(coord, fake_tg, {222: TelegramError(403, "Forbidden: bot was blocked by the user")})
    with caplog.at_level(logging.WARNING):
        for i in range(4):
            coord.pending_dues = [due]
            coord._deliver_waits(clock.now + i * 30)
    assert attempts == [111, 222]  # one attempt at the copy, then never again
    assert coord.store.kv_get("esc_copy:b:t_1:1") == "gave_up"
    assert _stage(coord, due) == "escalation" and coord.pending_dues == []  # the wait could proceed
    acts = [a for a in coord.store.actions_since(0) if a["kind"] == "escalation_copy_failed"]
    assert len(acts) == 1 and (acts[0]["board"], acts[0]["task_id"]) == ("b", "t_1")
    assert "bot was blocked" in acts[0]["detail"]
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1 and "escalation copy" in warns[0].getMessage() and "blocked" in warns[0].getMessage()


def test_permanent_copy_rejection_gives_up_every_pending_card_of_the_group(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    dues = [_due(f"t_{i}", stage="escalation", options=FAT) for i in range(9)]
    attempts, _ = _track(coord, fake_tg, {222: TelegramError(400, "Bad Request: chat not found")})
    coord.pending_dues = list(dues)
    coord._deliver_waits(clock.now)
    assert attempts.count(222) == 1  # the first copy message fails, the rest are given up with it
    assert all(coord.store.kv_get(f"esc_copy:b:{d.task_id}:1") == "gave_up" for d in dues)


def test_owner_400_on_one_chunk_does_not_hold_back_the_next(tmp_path, fake_tg, clock, caplog):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock)
    dues = [_due(f"t_{i:02d}", options=FAT) for i in range(10)]  # three messages
    for d in dues:
        _wait_row(coord, d)
    fake_tg.fail_next = TelegramError(400, "Bad Request: can't parse message text")  # first chunk only
    coord.pending_dues = list(dues)
    with caplog.at_level(logging.WARNING):
        coord._deliver_waits(clock.now)
    assert fake_tg.count("send") == 2  # chunks 2 and 3 went out
    sent_ids = [i["task_id"] for r in _sent_rows(coord) for i in json.loads(r["items_json"])]
    assert sent_ids == [d.task_id for d in dues[-len(sent_ids):]]
    assert all(_stage(coord, d) == "reminder1" for d in dues if d.task_id in sent_ids)
    unsent = [d for d in dues if d.task_id not in sent_ids]
    assert unsent and all(_stage(coord, d) is None for d in unsent)  # the rejected chunk stays unmarked
    assert [d.task_id for d in coord.pending_dues] == [d.task_id for d in unsent]
    assert len([r for r in caplog.records if "rejected" in r.getMessage()]) == 1


def test_permanent_owner_rejection_is_one_attempt_per_pass_and_logged_once(tmp_path, fake_tg, clock, caplog):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock)
    dues = [_due(f"t_{i}", options=FAT) for i in range(10)]
    attempts, _ = _track(coord, fake_tg, {111: TelegramError(403, "Forbidden: bot was blocked by the user")})
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            coord.pending_dues = list(dues)
            coord._deliver_waits(clock.now)
    assert attempts == [111, 111, 111]  # not one per chunk, not consuming the cap
    assert len(coord.pending_dues) == 10  # owner reminders stay unsent
    assert len([r for r in caplog.records if "rejected" in r.getMessage()]) == 1


def test_escalation_copy_is_sent_when_the_owner_send_failed(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    due = _due("t_1", stage="escalation")
    _wait_row(coord, due)
    attempts, fail = _track(coord, fake_tg, {111: TelegramError(400, "chat not found")})
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222]
    assert _stage(coord, due) is None and coord.pending_dues == [due]
    del fail[111]
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222, 111]  # the copy is not repeated
    assert _stage(coord, due) == "escalation" and coord.pending_dues == []


def test_escalation_copy_is_sent_when_the_owner_has_no_chat_id(tmp_path, fake_tg, clock):
    from ke.messages import t

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    due = _due("t_1", owner="ghost", stage="escalation")
    _wait_row(coord, due)
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    (call,) = fake_tg.calls["send"]
    assert call["chat_id"] == 222 and call["text"].splitlines()[:2] == ["[Kanban - Empowered]", t("waits.copy.lead")]
    assert "ghost" not in call["text"]  # role names never reach Telegram
    assert _stage(coord, due) is None  # the owner never got it
    coord.pending_dues = [due]
    coord._deliver_waits(clock.now)
    assert fake_tg.count("send") == 1


def test_transport_failure_aborts_the_whole_pass(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [
        _due("t_1", owner="sponsor", stage="escalation"),
        _due("t_2", owner="ops", stage="reminder1"),
    ]
    attempts, _ = _track(coord, fake_tg, {111: OSError("network is down")})
    coord._deliver_waits(clock.now)
    assert attempts == [111]  # not the other owner, not the escalation copy
    assert len(coord.pending_dues) == 2


def test_transport_failure_in_the_veto_warning_skips_the_deliveries(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, veto_hooks=False)
    due = _due("t_1")
    coord.waits.collect_due = lambda now: [due]
    attempts, fail = _track(coord, fake_tg, {111: OSError("timed out")})
    coord.evaluate(1000.0)
    assert attempts == [111] and len(coord.pending_dues) == 1
    del fail[111]
    coord.evaluate(1001.0)  # Telegram unreachable: nothing is attempted during the transport back-off
    assert attempts == [111] and len(coord.pending_dues) == 1
    from ke.coordinator import TG_BACKOFF_S

    coord.evaluate(1000.0 + TG_BACKOFF_S)  # the veto warning is still backing off, the reminder goes out
    assert attempts == [111, 111] and coord.pending_dues == []


def test_http_errors_do_not_abort_the_pass(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock)
    coord.pending_dues = [_due("t_1", owner="sponsor"), _due("t_2", owner="ops")]
    attempts, _ = _track(coord, fake_tg, {111: TelegramError(400, "bad")})
    coord._deliver_waits(clock.now)
    assert attempts == [111, 222]


def test_at_most_three_messages_per_owner_per_pass(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    dues = [_due(f"t_{i:02d}", options=FAT) for i in range(30)]
    coord.pending_dues = list(dues)
    coord._deliver_waits(clock.now)
    assert fake_tg.count("send") == 3
    sent_ids = {i["task_id"] for r in _sent_rows(coord) for i in json.loads(r["items_json"])}
    assert [d.task_id for d in coord.pending_dues] == [d.task_id for d in dues if d.task_id not in sent_ids]
    assert 0 < len(sent_ids) < 30
    coord._deliver_waits(clock.now)  # the next evaluation carries on with the rest
    assert fake_tg.count("send") == 6


def test_cap_is_per_recipient(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    coord.pending_dues = [_due(f"a{i:02d}", options=FAT) for i in range(20)] + [
        _due(f"b{i:02d}", owner="ops", options=FAT) for i in range(20)
    ]
    coord._deliver_waits(clock.now)
    chats = [c["chat_id"] for c in fake_tg.calls["send"]]
    assert chats.count(111) == 3 and chats.count(222) == 3

def test_missing_chat_id_warns_once_and_does_not_mark_sent(tmp_path, fake_tg, clock, caplog):
    coord = _coord(tmp_path, fake_tg, clock)
    due = _due("t_1", owner="ghost")
    coord.store.upsert_wait(due.board, due.task_id, due.version, "ghost", 0)
    other = _due("t_2", owner="sponsor")
    coord.pending_dues = [due, other]
    with caplog.at_level(logging.WARNING):
        coord._deliver_waits(clock.now)
        coord.pending_dues = [due]
        coord._deliver_waits(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [111]  # the other owner still got theirs
    assert coord.store.latest_wait(due.board, due.task_id)["stage"] is None
    assert [d.task_id for d in coord.pending_dues] == ["t_1"]
    warns = [r for r in caplog.records if "ghost" in r.getMessage()]
    assert len(warns) == 1


def test_missing_escalate_to_chat_id_does_not_block_the_owner(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="nobody")
    coord.pending_dues = [_due("t_1", stage="escalation")]
    coord._deliver_waits(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [111]
    assert coord.pending_dues == []


def test_no_telegram_client_logs_once_and_keeps_dues(tmp_path, clock, caplog):
    coord = make_coordinator(tmp_path, clock=clock, owner_ids=dict(OWNERS))
    coord.pending_dues = [_due("t_1")]
    with caplog.at_level(logging.WARNING):
        coord._deliver_waits(clock.now)
        coord._deliver_waits(clock.now)
    assert len([r for r in caplog.records if "TELEGRAM_BOT_TOKEN" in r.getMessage()]) == 1
    assert len(coord.pending_dues) == 1


def test_oversized_group_is_split_and_each_message_has_its_own_items(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    dues = [_due(f"t_{i}", options=FAT) for i in range(10)]
    coord.pending_dues = dues
    coord._deliver_waits(clock.now)
    assert fake_tg.count("send") > 1
    rows = _sent_rows(coord)
    assert len(rows) == fake_tg.count("send")
    all_ids = [i["task_id"] for r in rows for i in json.loads(r["items_json"])]
    assert all_ids == [d.task_id for d in dues]
    for call, row in zip(fake_tg.calls["send"], rows):
        assert len(call["text"].encode("utf-16-le")) // 2 <= 4096
        items = json.loads(row["items_json"])
        if len(items) > 1:
            from ke.messages import circled

            assert f"{circled(len(items))} B" in call["text"] and f"do {items[-1]['task_id']}" in call["text"]
    assert coord.pending_dues == []


def test_rate_limit_stops_that_owner_but_not_the_others(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock)
    coord.pending_dues = [_due(f"t_{i}", options=FAT) for i in range(10)]
    coord.pending_dues.append(_due("t_ops", owner="ops"))
    fake_tg.fail_next = TelegramError(429, "slow down")
    coord._deliver_waits(clock.now)
    assert [c["chat_id"] for c in fake_tg.calls["send"]] == [222]  # sponsor's batch halted at once
    assert len(coord.pending_dues) == 10


def test_delivery_failure_does_not_stop_later_steps(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock)
    ran = []
    coord.register_step("guards", lambda now: ran.append("guards"))

    def boom():
        raise RuntimeError("x")

    coord._deliver_waits = lambda now: boom()
    coord.evaluate(clock.now)
    assert ran == ["guards"]


# -- veto hooks warning over Telegram -------------------------------------------
def test_veto_warning_is_sent_once_to_default_owner(tmp_path, fake_tg, clock):
    from ke.messages import render_config_alert

    coord = _coord(tmp_path, fake_tg, clock, veto_hooks=False)
    coord.evaluate(1.0)
    coord.evaluate(2.0)
    (call,) = fake_tg.calls["send"]
    assert call["chat_id"] == 111 and call["text"] == "[Kanban - Empowered]\n" + render_config_alert("veto")
    assert coord.store.kv_get("veto_hooks_warned") == "1"
    assert coord.store.kv_get("veto_hooks_warn_sent") == "1"


def test_veto_warning_send_failure_is_retried_with_backoff(tmp_path, fake_tg, clock):
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, veto_hooks=False)
    fake_tg.fail_next = TelegramError(500, "x")
    coord.evaluate(1000.0)
    assert coord.store.kv_get("veto_hooks_warned") == "1"  # logged
    assert coord.store.kv_get("veto_hooks_warn_sent") is None
    coord.evaluate(1030.0)
    coord.evaluate(1000.0 + 599)
    assert fake_tg.count("send") == 0  # still backing off (a failed attempt consumed fail_next)
    coord.evaluate(1000.0 + 600)
    assert fake_tg.count("send") == 1 and coord.store.kv_get("veto_hooks_warn_sent") == "1"


def test_veto_warning_waits_for_a_telegram_client(tmp_path, clock, fake_tg):
    coord = make_coordinator(tmp_path, clock=clock, veto_hooks=False, owner_ids=dict(OWNERS))
    coord.evaluate(1.0)
    assert coord.store.kv_get("veto_hooks_warn_sent") is None
    coord.telegram = fake_tg
    coord.evaluate(2.0)
    assert fake_tg.count("send") == 1


def test_veto_warning_without_default_owner_chat_is_logged_not_sent(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, veto_hooks=False, owner_ids={"ops": 222})
    coord.evaluate(1.0)
    assert fake_tg.count("send") == 0 and coord.store.kv_get("veto_hooks_warn_sent") is None


def test_veto_warning_not_sent_when_hooks_present(tmp_path, fake_tg, clock):
    coord = _coord(tmp_path, fake_tg, clock, veto_hooks=True)
    coord.evaluate(1.0)
    assert fake_tg.count("send") == 0


# -- plugin wiring --------------------------------------------------------------
def test_register_builds_the_client_only_with_a_token(load_plugin, monkeypatch):
    mod = load_plugin("ke_plugin_tg")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert mod._telegram_client() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    client = mod._telegram_client()
    assert type(client).__name__ == "TelegramClient"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    assert mod._telegram_client() is None


# -- store ----------------------------------------------------------------------
def test_store_sent_message_roundtrip(tmp_path):
    from ke.store import Store

    s = Store(tmp_path / "s.db")
    items = [{"board": "b", "task_id": "t", "version": 3}]
    s.add_sent_message(10, 99, "waits", items, 1234)
    assert s.get_sent_message(99, 10) == {
        "message_id": 10, "chat_id": 99, "kind": "waits", "items": items, "created_at": 1234,
    }
    assert s.get_sent_message(99, 11) is None


# -- a transport failure backs off delivery across evaluations (final review M-2) ---------------
def test_transport_failure_backs_off_delivery_across_evaluations(kanban_home, clock, fake_tg, tmp_path):
    from ke import hermes_api
    from ke.coordinator import TG_BACKOFF_S
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, api=hermes_api)
    tid, _ = _real_wait(coord, clock)
    clock.advance(1800)
    attempts, fail = _track(coord, fake_tg, {111: TelegramError(None, "timed out")})
    coord.evaluate(clock.now)
    assert attempts == [111]  # one blocking attempt, then Telegram counts as down
    del fail[111]
    for _ in range(TG_BACKOFF_S // 30 - 1):  # the next evaluations inside the back-off try nothing
        clock.advance(30)
        coord.evaluate(clock.now)
    assert attempts == [111] and coord.store.latest_wait("default", tid)["stage"] is None
    clock.advance(30)  # back-off over
    coord.evaluate(clock.now)
    assert attempts == [111, 111] and coord.store.latest_wait("default", tid)["stage"] == "reminder1"


def test_an_http_error_does_not_back_off_the_next_evaluation(kanban_home, clock, fake_tg, tmp_path):
    from ke import hermes_api
    from ke.telegram import TelegramError

    coord = _coord(tmp_path, fake_tg, clock, api=hermes_api)
    tid, _ = _real_wait(coord, clock)
    clock.advance(1800)
    attempts, fail = _track(coord, fake_tg, {111: TelegramError(500, "Internal Server Error")})
    coord.evaluate(clock.now)
    del fail[111]
    clock.advance(30)
    coord.evaluate(clock.now)  # Telegram answered: it is reachable, retried on the next evaluation
    assert attempts == [111, 111] and coord.store.latest_wait("default", tid)["stage"] == "reminder1"


# -- the escalated copy is not resolvable by its reader (final review M-3) ----------------------
@pytest.mark.parametrize("n", [1, 2])
def test_escalated_copy_does_not_tell_the_contact_to_reply_done(tmp_path, fake_tg, clock, n):
    from ke.messages import t

    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [_due(f"t_{i}", stage="escalation") for i in range(1, n + 1)]
    coord._deliver_waits(clock.now)
    copy = next(c for c in fake_tg.calls["send"] if c["chat_id"] == 222)["text"]
    owner_msg = next(c for c in fake_tg.calls["send"] if c["chat_id"] == 111)["text"]
    assert copy.splitlines()[:2] == ["[Kanban - Empowered]", t("waits.copy.lead")]
    assert "reply" not in copy.lower() and "↩️" not in copy
    # the owner's own reminder keeps its reply instructions
    assert ("Then reply" in owner_msg) if n == 1 else ("↩️ When replying, name the item:" in owner_msg)


def test_escalated_copy_footer_in_pt_br(tmp_path, fake_tg, clock, kanban_home, pt_br):  # noqa: F811
    coord = _coord(tmp_path, fake_tg, clock, escalate_to="ops")
    coord.pending_dues = [_due("t_1", stage="escalation")]
    coord._deliver_waits(clock.now)
    copy = next(c for c in fake_tg.calls["send"] if c["chat_id"] == 222)["text"]
    assert copy.splitlines()[:2] == ["[Kanban - Empowered]", "Cópia para acompanhamento"]
    assert "responda" not in copy.lower() and "sponsor" not in copy
