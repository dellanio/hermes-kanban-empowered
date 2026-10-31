import sqlite3

import pytest

from ke.store import Store


def _tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _cols(path, table):
    c = sqlite3.connect(path)
    try:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    finally:
        c.close()


def test_schema_created_with_wal(tmp_path):
    p = tmp_path / "s.db"
    s = Store(p)
    assert {
        "actions", "waits", "sent_messages", "processed_updates",
        "infra_undos", "guard_alerts", "kv",
    } <= _tables(p)
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    s.close()


def test_exact_columns(tmp_path):
    p = tmp_path / "s.db"
    Store(p).close()
    assert _cols(p, "actions") == ["id", "ts", "board", "task_id", "kind", "detail"]
    assert _cols(p, "waits") == [
        "board", "task_id", "version", "owner", "started_at", "stage", "last_sent_at",
    ]
    assert _cols(p, "sent_messages") == ["message_id", "chat_id", "kind", "items_json", "created_at"]
    assert _cols(p, "processed_updates") == ["update_key", "ts"]
    assert _cols(p, "infra_undos") == ["board", "task_id", "ts"]
    assert _cols(p, "guard_alerts") == [
        "board", "task_id", "reason", "first_seen", "last_seen", "alerted_at",
    ]
    assert _cols(p, "kv") == ["key", "value"]


def test_reopen_is_idempotent_and_keeps_data(tmp_path):
    p = tmp_path / "s.db"
    s = Store(p)
    s.kv_set("a", "1")
    s.close()
    s2 = Store(p)
    assert s2.kv_get("a") == "1"
    s2.close()


def test_creates_parent_dir(tmp_path):
    s = Store(tmp_path / "deep" / "er" / "s.db")
    assert s.kv_get("x") is None
    s.close()


def test_mark_update_dedupes(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.mark_update("tg:1:99", 1) is True
    assert s.mark_update("tg:1:99", 2) is False
    assert s.mark_update("tg:1:100", 2) is True


def test_kv_roundtrip_and_overwrite(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.kv_get("k") is None
    s.kv_set("k", "v1")
    s.kv_set("k", "v2")
    assert s.kv_get("k") == "v2"


def test_log_action_and_actions_since(tmp_path):
    s = Store(tmp_path / "s.db")
    s.log_action("default", "t1", "unblock", "why", 100)
    s.log_action("default", "t2", "assign", "x", 200)
    s.log_action("b2", "t3", "comment", None, 300)
    rows = s.actions_since(150)
    assert [r["task_id"] for r in rows] == ["t2", "t3"]
    assert rows[0]["kind"] == "assign" and rows[0]["board"] == "default"
    assert rows[0]["detail"] == "x" and rows[0]["ts"] == 200
    assert rows[1]["detail"] is None
    assert set(rows[0]) == {"id", "ts", "board", "task_id", "kind", "detail"}
    # inclusive lower bound
    assert [r["task_id"] for r in s.actions_since(200)] == ["t2", "t3"]
    assert s.actions_since(10_000) == []


def test_actions_since_ordered_by_ts_then_id(tmp_path):
    s = Store(tmp_path / "s.db")
    s.log_action("d", "b", "k", "", 5)
    s.log_action("d", "a", "k", "", 5)
    s.log_action("d", "c", "k", "", 1)
    assert [r["task_id"] for r in s.actions_since(0)] == ["c", "b", "a"]


def test_primary_keys_enforced(tmp_path):
    s = Store(tmp_path / "s.db")
    s.conn.execute("INSERT INTO waits VALUES ('d','t',1,'o',1,'s',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("INSERT INTO waits VALUES ('d','t',1,'o2',2,'s',NULL)")
    s.conn.execute("INSERT INTO sent_messages VALUES (1, 5, 'k', '[]', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("INSERT INTO sent_messages VALUES (1, 5, 'k', '[]', 2)")
    s.conn.execute("INSERT INTO sent_messages VALUES (1, 6, 'k', '[]', 2)")  # other chat ok
    s.conn.execute("INSERT INTO guard_alerts VALUES ('d','t','r',1,1,NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        s.conn.execute("INSERT INTO guard_alerts VALUES ('d','t','r2',1,1,NULL)")


def test_readonly_second_connection_while_open(tmp_path):
    p = tmp_path / "s.db"
    s = Store(p)
    s.kv_set("k", "v")
    ro = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert ro.execute("SELECT value FROM kv WHERE key='k'").fetchone()[0] == "v"
    finally:
        ro.close()
    s.close()
