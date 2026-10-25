"""Automatic syncing, the cross-process lock, preferences and time zones."""

import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from canvas_sync.config import ConfigError
from fakes import FakeCalendarService, FakeTasksService
from helpers import SAMPLE_FEED, csrf_token, db_rows, make_item, save_feed_and_token
from webapp import scheduler, sync_runner, views
from webapp.db import get_db, init_db, release_lock, try_lock, upsert_user


@pytest.fixture
def fake_google(monkeypatch):
    """Route syncs to fake Google services and the sample feed."""
    services = SimpleNamespace(calendar=FakeCalendarService(), tasks=FakeTasksService())
    monkeypatch.setattr(sync_runner, "fetch_feed", lambda url: SAMPLE_FEED)
    monkeypatch.setattr(sync_runner, "load_credentials", lambda db, box, uid: SimpleNamespace(token="t"))
    monkeypatch.setattr(sync_runner, "build_services", lambda creds: (services.calendar, services.tasks))
    return services


# --- Lock -------------------------------------------------------------------

def test_lock_is_exclusive_until_released(app):
    with app.app_context():
        db = get_db()
        assert try_lock(db, "sync:user:1")
        assert not try_lock(db, "sync:user:1")
        assert try_lock(db, "sync:user:2")  # other users aren't blocked
        release_lock(db, "sync:user:1")
        assert try_lock(db, "sync:user:1")


def test_lock_is_exclusive_across_separate_connections(app):
    # Separate connections stand in for separate processes (scheduler vs. web worker).
    first = sqlite3.connect(app.config["DATABASE_PATH"])
    second = sqlite3.connect(app.config["DATABASE_PATH"])
    for conn in (first, second):
        conn.row_factory = sqlite3.Row
    try:
        assert try_lock(first, "sync:user:1")
        assert not try_lock(second, "sync:user:1")
    finally:
        first.close()
        second.close()


def test_expired_lock_can_be_taken_over(app):
    # A sync that crashed without releasing its lock mustn't block syncing forever.
    with app.app_context():
        db = get_db()
        assert try_lock(db, "sync:user:1", ttl=-1)  # already expired
        assert try_lock(db, "sync:user:1")


def test_sync_refuses_to_overlap(app, user_id, fake_google):
    save_feed_and_token(app, user_id)
    with app.app_context():
        db = get_db()
        try_lock(db, f"sync:user:{user_id}")
        outcome = sync_runner.run_sync(db, app.config["SECRET_BOX"], user_id, dry_run=False)
    assert not outcome.ok and "already running" in outcome.error
    assert fake_google.tasks.writes == []


# --- sync_all_users / sync-all command -------------------------------------

def test_scheduled_sync_covers_opted_in_users_only(app, user_id, fake_google):
    save_feed_and_token(app, user_id)
    with app.app_context():
        db = get_db()
        other = upsert_user(db, "sub-2", "other@example.com")
        db.execute("UPDATE users SET auto_sync = 0 WHERE id = ?", (other,))
        db.commit()
    save_feed_and_token(app, other)

    results = scheduler.sync_all_users(app)
    assert [(r.email, r.ok) for r in results] == [("noah@example.com", True)]
    assert db_rows(app, "SELECT user_id, trigger, dry_run, ok FROM sync_runs") == [(user_id, "scheduled", 0, 1)]
    assert len(fake_google.tasks.tasks_by_id) == 3


def test_users_without_feed_or_token_are_skipped(app, user_id, fake_google):
    assert scheduler.sync_all_users(app) == []


def test_one_users_crash_does_not_stop_the_others(app, user_id, fake_google, monkeypatch):
    save_feed_and_token(app, user_id)
    with app.app_context():
        second = upsert_user(get_db(), "sub-2", "second@example.com")
    save_feed_and_token(app, second)

    real_run = sync_runner.run_sync

    def flaky(db, box, uid, **kwargs):
        if uid == user_id:
            raise KeyError("boom")
        return real_run(db, box, uid, **kwargs)

    monkeypatch.setattr(sync_runner, "run_sync", flaky)
    results = scheduler.sync_all_users(app)
    assert [(r.email, r.ok) for r in results] == [("noah@example.com", False), ("second@example.com", True)]


def test_sync_all_command_reports_and_sets_exit_code(app, user_id, fake_google):
    save_feed_and_token(app, user_id)
    result = app.test_cli_runner().invoke(args=["sync-all"])
    assert result.exit_code == 0 and "noah@example.com: ok" in result.output
    assert db_rows(app, "SELECT trigger FROM sync_runs") == [("cron",)]


def test_scheduler_thread_runs_and_stops(app, user_id, fake_google, monkeypatch):
    save_feed_and_token(app, user_id)
    monkeypatch.setattr(scheduler, "FIRST_RUN_DELAY_SECONDS", 0.05)
    stop = scheduler.start_scheduler(app, interval_hours=1)
    try:
        for _ in range(100):
            if db_rows(app, "SELECT COUNT(*) FROM sync_runs")[0][0]:
                break
            time.sleep(0.05)
    finally:
        stop.set()
    assert db_rows(app, "SELECT trigger, ok FROM sync_runs") == [("scheduled", 1)]


# --- Settings: interval, preferences, migration -----------------------------

def test_too_frequent_interval_is_rejected(monkeypatch):
    from webapp.settings import load_settings

    for name in ("FLASK_SECRET_KEY", "APP_ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAILS"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "0.01")
    with pytest.raises(ConfigError, match="at least"):
        load_settings()


def test_preferences_are_saved(logged_in, app, user_id):
    logged_in.post("/settings/preferences",
                   data={"timezone": "America/Chicago", "csrf_token": csrf_token(logged_in)})
    assert db_rows(app, "SELECT timezone, auto_sync FROM users") == [("America/Chicago", 0)]


def test_unknown_time_zone_is_rejected(logged_in, app, user_id):
    logged_in.post("/settings/preferences",
                   data={"timezone": "Mars/Olympus", "auto_sync": "on", "csrf_token": csrf_token(logged_in)})
    assert db_rows(app, "SELECT timezone FROM users") == [(None,)]


def test_dashboard_shows_times_in_users_time_zone(logged_in, app, user_id, monkeypatch):
    save_feed_and_token(app, user_id)
    item = make_item("event-assignment-1", "Late quiz", start=datetime(2099, 11, 3, 3, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(views, "fetch_feed", lambda url: b"")
    monkeypatch.setattr(views, "parse_feed", lambda data: [item])

    assert "Mon Nov 02 10:00 PM" in logged_in.get("/").get_data(as_text=True)  # default: New York
    logged_in.post("/settings/preferences", data={"timezone": "UTC", "csrf_token": csrf_token(logged_in)})
    assert "Tue Nov 03 03:00 AM" in logged_in.get("/").get_data(as_text=True)


def test_sync_uses_users_time_zone_not_the_servers(app, user_id, fake_google):
    # Honolulu (UTC-10) differs from any likely server/dev-machine zone. Homework 3
    # is due 2099-10-30 03:00 UTC = Oct 29 at 5:00 PM in Honolulu.
    save_feed_and_token(app, user_id)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE users SET timezone = 'Pacific/Honolulu' WHERE id = ?", (user_id,))
        db.commit()

    scheduler.sync_all_users(app)
    (task,) = [t for t in fake_google.tasks.tasks_by_id.values() if t["title"].startswith("Homework 3")]
    assert task["due"] == "2099-10-29T00:00:00.000Z"
    assert task["notes"].startswith("Due: Thu Oct 29, 2099 at 05:00 PM")


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE, "
        "email TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, started_at TEXT NOT NULL, "
        "finished_at TEXT, dry_run INTEGER NOT NULL, ok INTEGER, summary_json TEXT, error TEXT);"
        "INSERT INTO users VALUES (1, 'sub', 'a@b.c', 'now');"
    )
    conn.commit()
    conn.close()

    fake_app = SimpleNamespace(config={"DATABASE_PATH": path}, teardown_appcontext=lambda f: f)
    init_db(fake_app)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT email, timezone, auto_sync FROM users").fetchall() == [("a@b.c", None, 1)]
    assert "trigger" in {r[1] for r in conn.execute("PRAGMA table_info(sync_runs)")}
    conn.close()
