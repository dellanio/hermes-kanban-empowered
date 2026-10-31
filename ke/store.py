"""Canonical plugin state: a plain-file SQLite (WAL) database owned by the plugin.

Hermes' own kanban DBs are never written here; this holds only what the plugin
itself must remember (actions taken, waits, sent Telegram messages, processed
updates, infra-undo markers, guard alerts, small key/value settings).

The file is a plain SQLite DB, so another process can open it read-only
(``file:...?mode=ro``) while this one is writing.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    board   TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions (ts);

CREATE TABLE IF NOT EXISTS waits (
    board        TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    owner        TEXT,
    started_at   INTEGER NOT NULL,
    stage        TEXT,
    last_sent_at INTEGER,
    PRIMARY KEY (board, task_id, version)
);

CREATE TABLE IF NOT EXISTS sent_messages (
    message_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_key TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS infra_undos (
    board   TEXT NOT NULL,
    task_id TEXT NOT NULL,
    ts      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guard_alerts (
    board      TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    alerted_at INTEGER,
    PRIMARY KEY (board, task_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """Thin accessor over the plugin database.

    Autocommit connection (each statement is its own transaction) guarded by a
    lock so the gateway's threads may share one instance.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- actions ---------------------------------------------------------
    def log_action(
        self, board: str, task_id: str, kind: str, detail: Optional[str], ts: int
    ) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO actions (ts, board, task_id, kind, detail) VALUES (?,?,?,?,?)",
                (int(ts), board, task_id, kind, detail),
            )

    def actions_since(self, ts: int) -> list[dict]:
        """Actions with ``ts >= ts``, oldest first (ties broken by insertion order)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, board, task_id, kind, detail FROM actions "
                "WHERE ts >= ? ORDER BY ts ASC, id ASC",
                (int(ts),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- kv --------------------------------------------------------------
    def kv_get(self, key: str) -> Optional[str]:
        with self.lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- telegram update dedupe -----------------------------------------
    def mark_update(self, update_key: str, ts: int) -> bool:
        """Record ``update_key``; False if it had already been processed."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO processed_updates (update_key, ts) VALUES (?, ?)",
                (update_key, int(ts)),
            )
            return cur.rowcount == 1
