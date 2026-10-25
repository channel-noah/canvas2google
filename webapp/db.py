"""Database storage with SQLAlchemy Core.

The same code runs on SQLite (local development, tests) and Postgres (the cloud,
where the filesystem doesn't persist). Queries are built from Python objects,
never by pasting strings together, so data from users can't change the SQL
(SQL injection).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app, g
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool

from canvas_sync.config import restrict_permissions

DEFAULT_TIMEZONE = "America/New_York"
LOCK_TTL_SECONDS = 15 * 60  # a crashed sync's lock expires after this
SECRET_NAMES = ("feed_url", "google_token")
KEEP_RUNS = 20  # sync history kept per user

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("google_sub", Text, nullable=False, unique=True),  # Google's stable account ID
    Column("email", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("timezone", Text),
    Column("auto_sync", Integer, nullable=False, server_default="1"),
)
user_secrets = Table(  # values are encrypted (see crypto.py)
    "user_secrets", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("feed_url", LargeBinary),
    Column("google_token", LargeBinary),
)
sync_state = Table(
    "sync_state", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("state_json", Text, nullable=False),
)
sync_runs = Table(
    "sync_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("dry_run", Integer, nullable=False),
    Column("ok", Integer),
    Column("summary_json", Text),
    Column("error", Text),
    Column("trigger", Text),
)
job_locks = Table(  # one sync per user at a time, across processes and servers
    "job_locks", metadata,
    Column("name", Text, primary_key=True),
    Column("expires_at", Float, nullable=False),
)

# Columns added after the first local release. Old SQLite databases get them in
# place; new databases already have them from the table definitions above.
_MIGRATIONS = [
    ("users", "timezone", "ALTER TABLE users ADD COLUMN timezone TEXT"),
    ("users", "auto_sync", "ALTER TABLE users ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 1"),
    ("sync_runs", "trigger", "ALTER TABLE sync_runs ADD COLUMN trigger TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_engine(url: str) -> Engine:
    if url.startswith(("postgres://", "postgresql://")):
        # Hosts hand out plain postgres:// URLs; tell SQLAlchemy to use psycopg 3.
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    if url.startswith("sqlite"):
        # No pool: each request opens and closes its own connection, as in the
        # cloud, so no database file handles stay open between requests.
        engine = create_engine(url, poolclass=NullPool)
        path = engine.url.database
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys = ON")  # needed for ON DELETE CASCADE

        return engine
    # Serverless functions get frozen between requests, so pooled connections go
    # stale. Open a fresh one per request instead, with a connect timeout.
    return create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 10})


def init_db(app: Flask) -> None:
    engine = make_engine(app.config["DATABASE_URL"])
    app.extensions["db_engine"] = engine
    with engine.begin() as conn:
        metadata.create_all(conn)
        inspector = inspect(conn)
        for table, column, ddl in _MIGRATIONS:
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(ddl))
    if engine.url.get_backend_name() == "sqlite" and engine.url.database:
        restrict_permissions(Path(engine.url.database))
    app.teardown_appcontext(close_db)


def get_db() -> Connection:
    if "db" not in g:
        g.db = current_app.extensions["db_engine"].connect()
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _insert(db: Connection, table: Table):
    """INSERT that supports ON CONFLICT ("upsert") on both databases."""
    return pg_insert(table) if db.dialect.name == "postgresql" else sqlite_insert(table)


# --- Users ------------------------------------------------------------------

def upsert_user(db: Connection, google_sub: str, email: str) -> int:
    stmt = _insert(db, users).values(google_sub=google_sub, email=email, created_at=_now())
    db.execute(stmt.on_conflict_do_update(index_elements=["google_sub"], set_={"email": stmt.excluded.email}))
    db.commit()
    return db.execute(select(users.c.id).where(users.c.google_sub == google_sub)).scalar_one()


def get_user(db: Connection, user_id: int) -> dict | None:
    row = db.execute(
        select(
            users.c.id,
            users.c.email,
            func.coalesce(users.c.timezone, DEFAULT_TIMEZONE).label("timezone"),
            users.c.auto_sync,
        ).where(users.c.id == user_id)
    ).first()
    return dict(row._mapping) if row else None


def find_user_id(db: Connection, email: str) -> int | None:
    return db.execute(select(users.c.id).where(users.c.email == email.lower())).scalar_one_or_none()


def update_preferences(db: Connection, user_id: int, timezone: str, auto_sync: bool) -> None:
    db.execute(update(users).where(users.c.id == user_id).values(timezone=timezone, auto_sync=int(auto_sync)))
    db.commit()


def delete_user(db: Connection, user_id: int) -> None:
    """Delete a user and, through ON DELETE CASCADE, all their secrets, state and history."""
    db.execute(delete(users).where(users.c.id == user_id))
    db.commit()


def users_due_for_auto_sync(db: Connection) -> list[dict]:
    """Users with automatic sync on and both a feed URL and a Google token saved."""
    rows = db.execute(
        select(users.c.id, users.c.email)
        .join(user_secrets, user_secrets.c.user_id == users.c.id)
        .where(
            users.c.auto_sync == 1,
            user_secrets.c.feed_url.is_not(None),
            user_secrets.c.google_token.is_not(None),
        )
        .order_by(users.c.id)
    )
    return [dict(r._mapping) for r in rows]


# --- Cross-process lock ---------------------------------------------------------

def try_lock(db: Connection, name: str, ttl: float = LOCK_TTL_SECONDS) -> bool:
    """Take the named lock unless someone else holds an unexpired one.

    One atomic statement: insert the lock, or take over an *expired* one. If an
    unexpired lock exists, the WHERE clause blocks the update and no row comes
    back. Both SQLite and Postgres run this as a single step, so two processes
    can never both get the lock.
    """
    now = time.time()
    stmt = _insert(db, job_locks).values(name=name, expires_at=now + ttl)
    stmt = stmt.on_conflict_do_update(
        index_elements=["name"],
        set_={"expires_at": stmt.excluded.expires_at},
        where=job_locks.c.expires_at < now,
    ).returning(job_locks.c.name)
    acquired = db.execute(stmt).first() is not None
    db.commit()
    return acquired


def release_lock(db: Connection, name: str) -> None:
    db.execute(delete(job_locks).where(job_locks.c.name == name))
    db.commit()


# --- Encrypted secrets --------------------------------------------------------

def get_secret(db: Connection, user_id: int, name: str) -> bytes | None:
    column = user_secrets.c[_check_secret_name(name)]
    value = db.execute(select(column).where(user_secrets.c.user_id == user_id)).scalar_one_or_none()
    return bytes(value) if value is not None else None


def set_secret(db: Connection, user_id: int, name: str, blob: bytes) -> None:
    name = _check_secret_name(name)
    stmt = _insert(db, user_secrets).values(user_id=user_id, **{name: blob})
    db.execute(stmt.on_conflict_do_update(index_elements=["user_id"], set_={name: getattr(stmt.excluded, name)}))
    db.commit()


def _check_secret_name(name: str) -> str:
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret: {name}")
    return name


# --- Sync state and history ---------------------------------------------------

def load_state(db: Connection, user_id: int) -> dict | None:
    value = db.execute(select(sync_state.c.state_json).where(sync_state.c.user_id == user_id)).scalar_one_or_none()
    return json.loads(value) if value else None


def save_state(db: Connection, user_id: int, data: dict) -> None:
    stmt = _insert(db, sync_state).values(user_id=user_id, state_json=json.dumps(data))
    db.execute(stmt.on_conflict_do_update(index_elements=["user_id"], set_={"state_json": stmt.excluded.state_json}))
    db.commit()


def start_run(db: Connection, user_id: int, dry_run: bool, trigger: str = "manual") -> int:
    result = db.execute(
        sync_runs.insert().values(user_id=user_id, started_at=_now(), dry_run=int(dry_run), trigger=trigger)
    )
    db.commit()
    return result.inserted_primary_key[0]


def finish_run(db: Connection, run_id: int, ok: bool, summary: dict | None, error: str | None) -> None:
    db.execute(
        update(sync_runs)
        .where(sync_runs.c.id == run_id)
        .values(finished_at=_now(), ok=int(ok), summary_json=json.dumps(summary) if summary else None, error=error)
    )
    user_id = db.execute(select(sync_runs.c.user_id).where(sync_runs.c.id == run_id)).scalar_one()
    prune_runs(db, user_id)
    db.commit()


def prune_runs(db: Connection, user_id: int, keep: int = KEEP_RUNS) -> None:
    """Delete all but the user's `keep` most recent runs.

    Run summaries include assignment titles, so old history is personal data we
    don't need. Keeping only recent runs is data minimization.
    """
    newest = (
        select(sync_runs.c.id)
        .where(sync_runs.c.user_id == user_id)
        .order_by(sync_runs.c.id.desc())
        .limit(keep)
        .scalar_subquery()
    )
    db.execute(delete(sync_runs).where(sync_runs.c.user_id == user_id, sync_runs.c.id.not_in(newest)))


def recent_runs(db: Connection, user_id: int, limit: int = 5) -> list[dict]:
    rows = db.execute(
        select(
            sync_runs.c.started_at,
            sync_runs.c.dry_run,
            sync_runs.c.ok,
            sync_runs.c.summary_json,
            sync_runs.c.error,
            func.coalesce(sync_runs.c.trigger, "manual").label("trigger"),
        )
        .where(sync_runs.c.user_id == user_id)
        .order_by(sync_runs.c.id.desc())
        .limit(limit)
    )
    return [
        {**dict(r._mapping), "summary": json.loads(r.summary_json) if r.summary_json else None}
        for r in rows
    ]
