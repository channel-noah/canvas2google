"""SQLite storage. Every query uses ? placeholders, never string formatting,
so data from users can't change the SQL (SQL injection)."""

from __future__ import annotations

import json
import sqlite3
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
"""

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
    return db.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()


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


def start_run(db: sqlite3.Connection, user_id: int, dry_run: bool) -> int:
    cur = db.execute(
        "INSERT INTO sync_runs (user_id, started_at, dry_run) VALUES (?, ?, ?)",
        (user_id, _now(), int(dry_run)),
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
        "SELECT started_at, dry_run, ok, summary_json, error FROM sync_runs "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {**dict(r), "summary": json.loads(r["summary_json"]) if r["summary_json"] else None}
        for r in rows
    ]
