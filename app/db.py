"""SQLite-opslag. Eén verbinding, beschermd met een lock (korte queries)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    user_name      TEXT NOT NULL,
    channel_id     TEXT,
    question       TEXT NOT NULL,
    answer         TEXT NOT NULL DEFAULT '',
    images         TEXT NOT NULL DEFAULT '[]',
    model          TEXT,
    status         TEXT NOT NULL DEFAULT 'ok',
    error          TEXT,
    memory_checked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, id);
CREATE TABLE IF NOT EXISTS facts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    text               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'approved',
    source             TEXT NOT NULL DEFAULT 'handmatig',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    discord_message_id TEXT,
    conversation_id    INTEGER
);
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level      TEXT NOT NULL,
    source     TEXT NOT NULL,
    message    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(db_path: Path | None = None) -> None:
    global _conn
    db_path = db_path or paths.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    paths.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()


def insert(sql: str, params: tuple | dict = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        return int(cur.lastrowid or 0)


# ---------- gemaksfuncties ----------

def get_json(table_value: str | None, default: Any) -> Any:
    if table_value is None:
        return default
    try:
        return json.loads(table_value)
    except (TypeError, ValueError):
        return default


def add_log(level: str, source: str, message: str, keep: int = 1000) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO logs (created_at, level, source, message) VALUES (?,?,?,?)",
            (now(), level, source, message[:4000]),
        )
        c.execute(
            "DELETE FROM logs WHERE id <= (SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (keep,),
        )
