"""Automatic syncing.

`sync_all_users` is the one job. It can be triggered two ways, so it works on
any host:
- a background thread inside the web app (`start_scheduler`), for hosts where
  the app runs all the time, and
- `flask --app webapp sync-all`, for an external scheduler such as cron.

The per-user database lock in sync_runner keeps the two from overlapping.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from flask import Flask

from . import sync_runner
from .db import get_db, users_due_for_auto_sync

log = logging.getLogger(__name__)

FIRST_RUN_DELAY_SECONDS = 60  # let the server finish starting up first


@dataclass
class UserRun:
    email: str
    ok: bool
    error: str | None


def sync_all_users(app: Flask, trigger: str = "scheduled") -> list[UserRun]:
    """Sync every user who has automatic sync on. One user's failure doesn't stop the rest."""
    results = []
    with app.app_context():
        db = get_db()
        box = app.config["SECRET_BOX"]
        for user in users_due_for_auto_sync(db):
            try:
                outcome = sync_runner.run_sync(db, box, user["id"], dry_run=False, trigger=trigger)
                results.append(UserRun(user["email"], outcome.ok, outcome.error))
            except Exception as exc:  # keep going for the other users
                log.warning("Scheduled sync crashed for user %s (%s)", user["id"], type(exc).__name__)
                results.append(UserRun(user["email"], False, type(exc).__name__))
    return results


def start_scheduler(app: Flask, interval_hours: float) -> threading.Event:
    """Run sync_all_users every `interval_hours` in a background thread.

    Returns an Event; set it to stop the thread (used by tests).
    """
    stop = threading.Event()

    def loop() -> None:
        wait = FIRST_RUN_DELAY_SECONDS
        while not stop.wait(wait):
            results = sync_all_users(app)
            failed = sum(not r.ok for r in results)
            log.info("Scheduled sync: %d user(s), %d failed", len(results), failed)
            wait = interval_hours * 3600

    # A daemon thread stops automatically when the server process exits.
    threading.Thread(target=loop, name="sync-scheduler", daemon=True).start()
    log.info("Automatic sync every %s hour(s)", interval_hours)
    return stop
