"""SQLite storage. Every query uses ? placeholders, never string formatting,
so data from users can't change the SQL (SQL injection)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app, g

from canvas_sync.config import restrict_permissions

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    google_sub  TEXT NOT NULL UNIQUE,   -- Google's stable account ID
    email       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_secrets (   -- values are encrypted (see crypto.py)
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    feed_url      BLOB,
    google_token  BLOB
);
CREATE TABLE IF NOT EXISTS sync_state (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    state_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    dry_run       INTEGER NOT NULL,
    ok            INTEGER,
    summary_json  TEXT,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS job_locks (   -- one sync per user at a time, across processes
    name        TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL
);
"""

# Columns added after the first release. Each runs once, if the column is missing,
# so existing databases are upgraded in place.
_MIGRATIONS = [
    ("users", "timezone", "ALTER TABLE users ADD COLUMN timezone TEXT"),
    ("users", "auto_sync", "ALTER TABLE users ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 1"),
    ("sync_runs", "trigger", "ALTER TABLE sync_runs ADD COLUMN trigger TEXT"),
]
_TABLE_INFO = {"users": "PRAGMA table_info(users)", "sync_runs": "PRAGMA table_info(sync_runs)"}

DEFAULT_TIMEZONE = "America/New_York"
LOCK_TTL_SECONDS = 15 * 60  # a crashed sync's lock expires after this

# Column names can't be "?" placeholders, so each secret gets fixed SQL.
_SECRET_SQL = {
    "feed_url": (
        "SELECT feed_url FROM user_secrets WHERE user_id = ?",
        "INSERT INTO user_secrets (user_id, feed_url) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET feed_url = excluded.feed_url",
    ),
    "google_token": (
        "SELECT google_token FROM user_secrets WHERE user_id = ?",
        "INSERT INTO user_secrets (user_id, google_token) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET google_token = excluded.google_token",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(app: Flask) -> None:
    path = app.config["DATABASE_PATH"]
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for table, column, sql in _MIGRATIONS:
            columns = {row["name"] for row in conn.execute(_TABLE_INFO[table])}
            if column not in columns:
                conn.execute(sql)
    conn.close()
    restrict_permissions(path)
    app.teardown_appcontext(close_db)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- Users ------------------------------------------------------------------

def upsert_user(db: sqlite3.Connection, google_sub: str, email: str) -> int:
    db.execute(
        "INSERT INTO users (google_sub, email, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(google_sub) DO UPDATE SET email = excluded.email",
        (google_sub, email, _now()),
    )
    db.commit()
    return db.execute("SELECT id FROM users WHERE google_sub = ?", (google_sub,)).fetchone()["id"]


def get_user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT id, email, COALESCE(timezone, ?) AS timezone, auto_sync FROM users WHERE id = ?",
        (DEFAULT_TIMEZONE, user_id),
    ).fetchone()


def update_preferences(db: sqlite3.Connection, user_id: int, timezone: str, auto_sync: bool) -> None:
    db.execute(
        "UPDATE users SET timezone = ?, auto_sync = ? WHERE id = ?", (timezone, int(auto_sync), user_id)
    )
    db.commit()


def users_due_for_auto_sync(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Users with automatic sync on and both a feed URL and a Google token saved."""
    return db.execute(
        "SELECT u.id, u.email FROM users u JOIN user_secrets s ON s.user_id = u.id "
        "WHERE u.auto_sync = 1 AND s.feed_url IS NOT NULL AND s.google_token IS NOT NULL "
        "ORDER BY u.id"
    ).fetchall()


# --- Cross-process lock ---------------------------------------------------------

def try_lock(db: sqlite3.Connection, name: str, ttl: float = LOCK_TTL_SECONDS) -> bool:
    """Take the named lock unless someone else holds an unexpired one.

    BEGIN IMMEDIATE makes SQLite grant the write lock to one connection at a
    time, so two processes (say, the scheduler and a button click) can't both
    see the lock as free.
    """
    now = time.time()
    db.commit()  # make sure no earlier transaction is still open
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT expires_at FROM job_locks WHERE name = ?", (name,)).fetchone()
        if row and row["expires_at"] > now:
            db.rollback()
            return False
        db.execute(
            "INSERT INTO job_locks (name, expires_at) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET expires_at = excluded.expires_at",
            (name, now + ttl),
        )
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise


def release_lock(db: sqlite3.Connection, name: str) -> None:
    db.execute("DELETE FROM job_locks WHERE name = ?", (name,))
    db.commit()


# --- Encrypted secrets --------------------------------------------------------

def get_secret(db: sqlite3.Connection, user_id: int, name: str) -> bytes | None:
    row = db.execute(_SECRET_SQL[name][0], (user_id,)).fetchone()
    return row[0] if row else None


def set_secret(db: sqlite3.Connection, user_id: int, name: str, blob: bytes) -> None:
    db.execute(_SECRET_SQL[name][1], (user_id, blob))
    db.commit()


# --- Sync state and history ---------------------------------------------------

def load_state(db: sqlite3.Connection, user_id: int) -> dict | None:
    row = db.execute("SELECT state_json FROM sync_state WHERE user_id = ?", (user_id,)).fetchone()
    return json.loads(row["state_json"]) if row else None


def save_state(db: sqlite3.Connection, user_id: int, data: dict) -> None:
    db.execute(
        "INSERT INTO sync_state (user_id, state_json) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json",
        (user_id, json.dumps(data)),
    )
    db.commit()


def start_run(db: sqlite3.Connection, user_id: int, dry_run: bool, trigger: str = "manual") -> int:
    cur = db.execute(
        "INSERT INTO sync_runs (user_id, started_at, dry_run, trigger) VALUES (?, ?, ?, ?)",
        (user_id, _now(), int(dry_run), trigger),
    )
    db.commit()
    return cur.lastrowid


def finish_run(db: sqlite3.Connection, run_id: int, ok: bool, summary: dict | None, error: str | None) -> None:
    db.execute(
        "UPDATE sync_runs SET finished_at = ?, ok = ?, summary_json = ?, error = ? WHERE id = ?",
        (_now(), int(ok), json.dumps(summary) if summary else None, error, run_id),
    )
    db.commit()


def recent_runs(db: sqlite3.Connection, user_id: int, limit: int = 5) -> list[dict]:
    rows = db.execute(
        "SELECT started_at, dry_run, ok, summary_json, error, COALESCE(trigger, 'manual') AS trigger FROM sync_runs "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {**dict(r), "summary": json.loads(r["summary_json"]) if r["summary_json"] else None}
        for r in rows
    ]
