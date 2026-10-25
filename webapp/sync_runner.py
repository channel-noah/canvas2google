"""Run a sync for one user, using their stored (encrypted) secrets.

Shared by the "Sync now"/"Preview" buttons and, later, the scheduler.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from canvas_sync.calendar_sync import EventSyncResult, sync_events
from canvas_sync.config import ConfigError
from canvas_sync.feed import FeedError, fetch_feed, parse_feed
from canvas_sync.google_client import build_services
from canvas_sync.state import StateError, SyncState
from canvas_sync.tasks_sync import TaskSyncResult, sync_tasks

from . import db as store
from .crypto import SecretBox, SecretError, secret_context


@dataclass
class SyncOutcome:
    ok: bool
    dry_run: bool
    events: EventSyncResult | None = None
    tasks: TaskSyncResult | None = None
    error: str | None = None
    needs_sign_in: bool = False


def load_feed_url(db: sqlite3.Connection, box: SecretBox, user_id: int) -> str | None:
    blob = store.get_secret(db, user_id, "feed_url")
    return box.decrypt(blob, secret_context(user_id, "feed_url")) if blob else None


def load_credentials(db: sqlite3.Connection, box: SecretBox, user_id: int) -> Credentials | None:
    blob = store.get_secret(db, user_id, "google_token")
    if not blob:
        return None
    info = json.loads(box.decrypt(blob, secret_context(user_id, "google_token")))
    return Credentials.from_authorized_user_info(info)


def _save_credentials(db: sqlite3.Connection, box: SecretBox, user_id: int, creds: Credentials) -> None:
    store.set_secret(db, user_id, "google_token", box.encrypt(creds.to_json(), secret_context(user_id, "google_token")))


def user_timezone(db: sqlite3.Connection, user_id: int) -> ZoneInfo:
    user = store.get_user(db, user_id)
    try:
        return ZoneInfo(user["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(store.DEFAULT_TIMEZONE)


def run_sync(
    db: sqlite3.Connection, box: SecretBox, user_id: int, dry_run: bool, trigger: str = "manual"
) -> SyncOutcome:
    # One sync per user at a time, even across processes (scheduler, cron job,
    # button click). Two overlapping runs could both decide an item is new and
    # create it twice.
    lock = f"sync:user:{user_id}"
    if not store.try_lock(db, lock):
        return SyncOutcome(ok=False, dry_run=dry_run, error="A sync is already running. Try again in a minute.")
    try:
        return _run_locked(db, box, user_id, dry_run, trigger)
    finally:
        store.release_lock(db, lock)


def _run_locked(
    db: sqlite3.Connection, box: SecretBox, user_id: int, dry_run: bool, trigger: str
) -> SyncOutcome:
    run_id = store.start_run(db, user_id, dry_run, trigger)
    outcome = SyncOutcome(ok=False, dry_run=dry_run)
    creds = None
    try:
        feed_url = load_feed_url(db, box, user_id)
        creds = load_credentials(db, box, user_id)
        if not feed_url:
            outcome.error = "Add your Canvas feed URL in Settings first."
        elif not creds:
            outcome.error, outcome.needs_sign_in = "Please sign in to Google again.", True
        else:
            token_before = creds.token
            items = parse_feed(fetch_feed(feed_url))
            saved = store.load_state(db, user_id)

            def saver(data: dict) -> None:
                store.save_state(db, user_id, data)

            state = SyncState.from_dict(saved, saver=saver) if saved else SyncState(path=None, saver=saver)
            tz = user_timezone(db, user_id)
            calendar, tasks = build_services(creds)
            outcome.events = sync_events(calendar, items, state, dry_run=dry_run, tz=tz)
            outcome.tasks = sync_tasks(tasks, items, state, dry_run=dry_run, tz=tz)
            outcome.ok = True
            if creds.token != token_before:  # the library refreshed the access token
                _save_credentials(db, box, user_id, creds)
    except (ConfigError, FeedError, StateError, SecretError) as exc:
        outcome.error = str(exc)
    except RefreshError:
        outcome.error, outcome.needs_sign_in = "Google access expired or was revoked. Please sign in again.", True
    except HttpError as exc:
        outcome.error = f"Google refused a request (HTTP {exc.resp.status}). Progress so far is saved."
    except (OSError, HttpLib2Error, GoogleAuthError) as exc:
        outcome.error = f"Couldn't reach Google ({type(exc).__name__}). Progress so far is saved."
    finally:
        summary = None
        if outcome.events or outcome.tasks:
            summary = {
                "events": dataclasses.asdict(outcome.events) if outcome.events else None,
                "tasks": dataclasses.asdict(outcome.tasks) if outcome.tasks else None,
            }
        store.finish_run(db, run_id, outcome.ok, summary, outcome.error)
    return outcome
