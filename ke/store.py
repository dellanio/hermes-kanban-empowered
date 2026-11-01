"""Canonical plugin state: a plain-file SQLite (WAL) database owned by the plugin.

Hermes' own kanban DBs are never written here; this holds only what the plugin
itself must remember (actions taken, waits, sent Telegram messages, processed
updates, infra-undo markers, guard alerts, small key/value settings).

The file is a plain SQLite DB, so another process can open it read-only
(``file:...?mode=ro``) while this one is writing.
"""
from __future__ import annotations

import json
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

    def action_exists(self, board: str, task_id: str, kind: str, detail: str) -> bool:
        """True when an action with exactly this ``(board, task_id, kind, detail)`` was logged."""
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM actions WHERE board = ? AND task_id = ? AND kind = ? AND detail = ? LIMIT 1",
                (board, task_id, kind, detail),
            ).fetchone()
        return row is not None

    def count_actions_since(self, kind: str, ts: int, detail_prefix: str = "") -> int:
        """Actions of ``kind`` with ``ts >= ts`` whose detail starts with ``detail_prefix``."""
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE kind = ? AND ts >= ? AND substr(detail, 1, ?) = ?",
                (kind, int(ts), len(detail_prefix), detail_prefix),
            ).fetchone()
        return int(row["n"])

    # -- infra undo budget -------------------------------------------------
    def add_infra_undo(self, board: str, task_id: str, ts: int) -> int:
        """Record one undo; returns its row id (for :meth:`delete_infra_undo`)."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO infra_undos (board, task_id, ts) VALUES (?,?,?)", (board, task_id, int(ts))
            )
            return int(cur.lastrowid)

    def delete_infra_undo(self, row_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM infra_undos WHERE rowid = ?", (int(row_id),))

    def infra_undos_since(self, board: str, task_id: str, ts: float) -> int:
        """How many times the block of this card was undone at or after ``ts``."""
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM infra_undos WHERE board = ? AND task_id = ? AND ts >= ?",
                (board, task_id, float(ts)),
            ).fetchone()
        return int(row["n"])

    # -- waits (read side; rows are written by the wait tracker) ---------
    def latest_wait(self, board: str, task_id: str) -> Optional[dict]:
        """The highest-``version`` wait row of a card, or ``None``."""
        with self.lock:
            row = self.conn.execute(
                "SELECT board, task_id, version, owner, started_at, stage, last_sent_at "
                "FROM waits WHERE board = ? AND task_id = ? ORDER BY version DESC LIMIT 1",
                (board, task_id),
            ).fetchone()
        return None if row is None else dict(row)

    def all_waits(self) -> list[dict]:
        """Every wait row, ordered by board, card and version."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT board, task_id, version, owner, started_at, stage, last_sent_at "
                "FROM waits ORDER BY board, task_id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_wait(
        self, board: str, task_id: str, version: int, owner: Optional[str], started_at: int
    ) -> None:
        """Insert a wait, or refresh only its owner: ``stage``/``last_sent_at`` are never reset."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO waits (board, task_id, version, owner, started_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(board, task_id, version) DO UPDATE SET owner = excluded.owner",
                (board, task_id, int(version), owner, int(started_at)),
            )

    def delete_wait(self, board: str, task_id: str, version: int) -> None:
        with self.lock:
            self.conn.execute(
                "DELETE FROM waits WHERE board = ? AND task_id = ? AND version = ?",
                (board, task_id, int(version)),
            )

    def mark_wait_sent(self, board: str, task_id: str, version: int, stage: str, ts: int) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE waits SET stage = ?, last_sent_at = ? "
                "WHERE board = ? AND task_id = ? AND version = ?",
                (stage, int(ts), board, task_id, int(version)),
            )

    # -- sent telegram messages (reply mapping) --------------------------
    def add_sent_message(
        self, message_id: int, chat_id: int, kind: str, items: list, created_at: int
    ) -> None:
        """Remember a message we sent; ``items`` is its numbered item list, in numbering order."""
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sent_messages "
                "(message_id, chat_id, kind, items_json, created_at) VALUES (?,?,?,?,?)",
                (int(message_id), int(chat_id), kind, json.dumps(items), int(created_at)),
            )

    def get_sent_message(self, chat_id: int, message_id: int) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT message_id, chat_id, kind, items_json, created_at FROM sent_messages "
                "WHERE chat_id = ? AND message_id = ?",
                (int(chat_id), int(message_id)),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["items"] = json.loads(out.pop("items_json"))
        return out

    # -- guard alerts ------------------------------------------------------
    _GUARD_COLS = "board, task_id, reason, first_seen, last_seen, alerted_at"

    def upsert_guard_alert(self, board: str, task_id: str, reason: str, now: int) -> None:
        """Record that ``task_id`` was seen guarded for ``reason`` at ``now``.

        A new card starts the clock; the same reason only refreshes ``last_seen``; a different
        reason is a new episode: ``first_seen`` restarts and the alert is owed again.
        """
        with self.lock:
            self.conn.execute(
                "INSERT INTO guard_alerts (board, task_id, reason, first_seen, last_seen) "
                "VALUES (?,?,?,?,?) ON CONFLICT(board, task_id) DO UPDATE SET "
                "first_seen = CASE WHEN reason = excluded.reason THEN first_seen ELSE excluded.first_seen END, "
                "alerted_at = CASE WHEN reason = excluded.reason THEN alerted_at ELSE NULL END, "
                "last_seen = excluded.last_seen, reason = excluded.reason",
                (board, task_id, reason, int(now), int(now)),
            )

    def get_guard_alert(self, board: str, task_id: str) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                f"SELECT {self._GUARD_COLS} FROM guard_alerts WHERE board = ? AND task_id = ?",
                (board, task_id),
            ).fetchone()
        return None if row is None else dict(row)

    def guard_alerts(self, board: Optional[str] = None) -> list[dict]:
        """Tracked guard rows (of one board, or all), ordered by board and ``first_seen``."""
        sql = f"SELECT {self._GUARD_COLS} FROM guard_alerts"
        args: tuple = ()
        if board is not None:
            sql += " WHERE board = ?"
            args = (board,)
        with self.lock:
            rows = self.conn.execute(sql + " ORDER BY board, first_seen, task_id", args).fetchall()
        return [dict(r) for r in rows]

    def due_guard_alerts(self, first_seen_before: float) -> list[dict]:
        """Rows never alerted whose ``first_seen`` is at or before ``first_seen_before``."""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._GUARD_COLS} FROM guard_alerts "
                "WHERE alerted_at IS NULL AND first_seen <= ? ORDER BY board, first_seen, task_id",
                (first_seen_before,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_guard_alerted(self, board: str, task_id: str, reason: str, ts: int) -> bool:
        """Mark the alert sent, only while the row still has ``reason`` (False otherwise)."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE guard_alerts SET alerted_at = ? WHERE board = ? AND task_id = ? AND reason = ?",
                (int(ts), board, task_id, reason),
            )
            return cur.rowcount == 1

    def delete_guard_alert(self, board: str, task_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "DELETE FROM guard_alerts WHERE board = ? AND task_id = ?", (board, task_id)
            )

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

    def kv_delete_prefix(self, prefix: str) -> int:
        """Delete every kv key that starts with ``prefix`` (a literal prefix, no wildcards); returns how many."""
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
            )
            return cur.rowcount

    # -- telegram update dedupe -----------------------------------------
    def mark_update(self, update_key: str, ts: int) -> bool:
        """Record ``update_key``; False if it had already been processed."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO processed_updates (update_key, ts) VALUES (?, ?)",
                (update_key, int(ts)),
            )
            return cur.rowcount == 1
