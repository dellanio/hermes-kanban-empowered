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

# The board (project) statuses the plugin knows. A board without a row is ``active``.
BOARD_STATUSES = ("active", "suspended", "done")

# Schema migrations, applied in order on open while ``PRAGMA user_version`` is below their number.
# Version 1 is ``_SCHEMA`` above (every statement idempotent). Each step runs in its own ``BEGIN IMMEDIATE``
# transaction that re-reads ``user_version`` first, so a step is applied once even when several processes
# open an old file together, and a failing step leaves nothing behind. Keep steps idempotent anyway
# (``IF NOT EXISTS``): it costs nothing and survives a hand-edited ``user_version``.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (2, """
CREATE TABLE IF NOT EXISTS board_status (
    board      TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'done')),
    changed_at INTEGER NOT NULL,
    changed_by TEXT
);
"""),
)
SCHEMA_VERSION = _MIGRATIONS[-1][0]


_BLANK = " \t\r\n;"  # what is left of an empty statement


def _statements(script: str) -> list[str]:
    """The SQL statements of ``script`` (split on statement-ending semicolons, quotes respected)."""
    out: list[str] = []
    buf = ""
    for part in script.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(_BLANK):
                out.append(buf.strip())
            buf = ""
    if buf.strip(_BLANK):
        out.append(buf.strip())
    return out


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
        self._migrate()

    def _migrate(self) -> None:
        """Bring the file to ``SCHEMA_VERSION`` (a no-op once there); one transaction per step."""
        with self.lock:
            if int(self.conn.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
                return
            for version, script in _MIGRATIONS:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    if int(self.conn.execute("PRAGMA user_version").fetchone()[0]) < version:
                        for statement in _statements(script):
                            self.conn.execute(statement)  # not executescript: it would commit on its own
                        self.conn.execute(f"PRAGMA user_version = {int(version)}")
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise

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

    def recent_actions(self, kinds, limit: int = 20, board: Optional[str] = None) -> list[dict]:
        """The newest ``limit`` actions whose kind is in ``kinds`` (of ``board`` only, when given), newest first."""
        kinds = list(kinds)
        if not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        where, args = f"kind IN ({marks})", list(kinds)
        if board is not None:
            where += " AND board = ?"
            args.append(board)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT id, ts, board, task_id, kind, detail FROM actions WHERE {where} "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (*args, int(limit)),
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

    def recent_sent_messages(self, kinds, limit: int = 20, board: Optional[str] = None) -> list[dict]:
        """The newest ``limit`` sent messages whose kind is in ``kinds`` (by ``created_at``), items parsed.

        ``board`` keeps only messages with an item of that board (a coarse ``items_json`` match in SQL, then
        an exact check). A live panel is one row whose ``created_at`` moves to its last update (see
        ``panel.record_items``).
        """
        kinds = list(kinds)
        if not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        where, args = f"kind IN ({marks})", list(kinds)
        if board is not None:
            escaped = board.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            where += " AND items_json LIKE ? ESCAPE '!'"
            args.append(f"%{escaped}%")
        out = []
        offset = 0
        while len(out) < limit:
            with self.lock:
                rows = self.conn.execute(
                    f"SELECT message_id, chat_id, kind, items_json, created_at FROM sent_messages "
                    f"WHERE {where} ORDER BY created_at DESC, message_id DESC LIMIT ? OFFSET ?",
                    (*args, int(limit), offset),
                ).fetchall()
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                item = dict(row)
                try:
                    item["items"] = json.loads(item.pop("items_json"))
                except ValueError:
                    item["items"] = []
                if board is not None and not any(
                    isinstance(i, dict) and i.get("board") == board for i in item["items"]
                ):
                    continue
                out.append(item)
        return out[:limit]

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

    # -- board (project) status ---------------------------------------------
    def board_status(self, board: str) -> str:
        """``active`` | ``suspended`` | ``done`` (``active`` for a board never set)."""
        with self.lock:
            row = self.conn.execute("SELECT status FROM board_status WHERE board = ?", (board,)).fetchone()
        return "active" if row is None else str(row["status"])

    def board_statuses(self) -> dict[str, dict]:
        """``{board: {status, changed_at, changed_by}}`` for every board with a recorded status."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT board, status, changed_at, changed_by FROM board_status ORDER BY board"
            ).fetchall()
        return {
            r["board"]: {"status": r["status"], "changed_at": int(r["changed_at"]), "changed_by": r["changed_by"]}
            for r in rows
        }

    def set_board_status(self, board: str, status: str, ts: int, changed_by: str) -> Optional[str]:
        """Set the status of ``board``; returns the previous status, or ``None`` when nothing changed.

        One transaction: the row, the ``board_status`` audit action (detail ``"<by>: <old>-><new>"``) and,
        on a transition to ``active``, the reset of the board's reminder ladder: every wait of the board
        forgets its sent stage and the escalation markers are dropped, so the ladder starts again from the
        reactivation time (``changed_at`` of the ``active`` row is its anchor, see ``waits.ladder_anchor``).
        Raises ValueError for a status outside ``BOARD_STATUSES``.
        """
        if status not in BOARD_STATUSES:
            raise ValueError(f"unknown board status {status!r}; expected one of {BOARD_STATUSES}")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT status FROM board_status WHERE board = ?", (board,)).fetchone()
                old = "active" if row is None else str(row["status"])
                if old == status:
                    self.conn.execute("COMMIT")
                    return None
                self.conn.execute(
                    "INSERT INTO board_status (board, status, changed_at, changed_by) VALUES (?,?,?,?) "
                    "ON CONFLICT(board) DO UPDATE SET status = excluded.status, "
                    "changed_at = excluded.changed_at, changed_by = excluded.changed_by",
                    (board, status, int(ts), changed_by),
                )
                self.conn.execute(
                    "INSERT INTO actions (ts, board, task_id, kind, detail) VALUES (?,?,?,?,?)",
                    (int(ts), board, "", "board_status", f"{changed_by}: {old}->{status}"),
                )
                if status == "active":
                    self.conn.execute(
                        "UPDATE waits SET stage = NULL, last_sent_at = NULL WHERE board = ?", (board,)
                    )
                    for kind in ("esc_owner", "esc_copy", "esc_copytry"):
                        prefix = f"{kind}:{board}:"
                        self.conn.execute(
                            "DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
                        )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return old

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
