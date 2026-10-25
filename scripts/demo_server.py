"""Local demo of the web app with fake data, for screenshots and walkthroughs.

    python scripts/demo_server.py [--fresh]      then open http://127.0.0.1:5050/demo-login

Nothing here touches real accounts: Canvas and Google are replaced with the
in-memory fakes from the test suite, and the database is a throwaway temp file.
The /demo-login shortcut exists only in this script, never in the real app.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from cryptography.fernet import Fernet  # noqa: E402
from flask import redirect, session  # noqa: E402

from fakes import FakeCalendarService, FakeTasksService  # noqa: E402
from webapp import create_app, scheduler, sync_runner, views  # noqa: E402
from webapp.crypto import secret_context  # noqa: E402
from webapp.db import get_db, set_secret, upsert_user  # noqa: E402
from webapp.settings import Settings  # noqa: E402

FRESH = "--fresh" in sys.argv  # start with nothing synced, to show a first Preview
DEMO_EMAIL = "demo.student@example.com"
DEMO_FEED_URL = "https://demo-university.instructure.com/feeds/calendars/user_DEMO.ics"


def _ics_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fake_feed() -> bytes:
    """A made-up semester, with dates relative to today so items are 'upcoming'."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    today = now.date()
    events = []

    def assignment(uid, title, course, days, hour=None):
        if hour is None:  # all-day = due 11:59 PM in Canvas
            day = today + timedelta(days=days)
            events.append(
                f"BEGIN:VEVENT\r\nUID:event-assignment-{uid}\r\n"
                f"DTSTART;VALUE=DATE:{day:%Y%m%d}\r\nDTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}\r\n"
                f"SUMMARY:{title} [{course}]\r\nEND:VEVENT\r\n"
            )
        else:
            due = (now + timedelta(days=days)).replace(hour=hour)
            events.append(
                f"BEGIN:VEVENT\r\nUID:event-assignment-{uid}\r\n"
                f"DTSTART:{_ics_time(due)}\r\nDTEND:{_ics_time(due)}\r\n"
                f"SUMMARY:{title} [{course}]\r\n"
                "DESCRIPTION:Submit a PDF on Canvas.\r\nEND:VEVENT\r\n"
            )

    def event(uid, title, course, days, hour, minutes):
        start = (now + timedelta(days=days)).replace(hour=hour)
        events.append(
            f"BEGIN:VEVENT\r\nUID:event-calendar-event-{uid}\r\n"
            f"DTSTART:{_ics_time(start)}\r\nDTEND:{_ics_time(start + timedelta(minutes=minutes))}\r\n"
            f"SUMMARY:{title} [{course}]\r\nEND:VEVENT\r\n"
        )

    assignment(101, "Lab 4: Hash Tables", "CS-201-01", 1, hour=3)
    assignment(102, "Problem Set 6", "MATH-210-02", 2)
    event(201, "Midterm review session", "CS-201-01", 3, 19, 90)
    assignment(103, "Reading response: Industrial Revolution", "HIST-150-03", 4)
    assignment(104, "Project 2: Graph Search", "CS-201-01", 6, hour=3)
    event(202, "Office hours", "MATH-210-02", 7, 15, 60)
    assignment(105, "Quiz 5", "PHYS-201-01", 8, hour=17)
    assignment(106, "Problem Set 7", "MATH-210-02", 9)
    assignment(107, "Essay draft: The Printing Press", "HIST-150-03", 11)
    assignment(108, "Lab 5: Heaps", "CS-201-01", 13, hour=3)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:demo\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def build_demo_app():
    if os.environ.get("VERCEL"):
        raise SystemExit("The demo server must never run on Vercel.")

    db_path = Path(tempfile.mkdtemp(prefix="canvas2google-demo-")) / "demo.db"
    settings = Settings(
        secret_key=Fernet.generate_key().decode(),
        encryption_key=Fernet.generate_key().decode(),
        google_client_id="demo.apps.googleusercontent.com",
        google_client_secret="demo",  # nosec B106: fake value; the demo never contacts Google
        allowed_emails=frozenset({DEMO_EMAIL}),
        database_url="sqlite:///" + db_path.as_posix(),
        cron_secret="d" * 40,
    )
    app = create_app(settings)

    # Swap the outside world for fakes.
    feed = fake_feed()
    services = (FakeCalendarService(), FakeTasksService())
    views.fetch_feed = sync_runner.fetch_feed = lambda url: feed
    sync_runner.load_credentials = lambda db, box, uid: SimpleNamespace(token="demo", refresh_token=None)  # nosec B106: fake credentials object
    sync_runner.build_services = lambda creds: services

    box = app.config["SECRET_BOX"]
    with app.app_context():
        db = get_db()
        user_id = upsert_user(db, "demo-sub", DEMO_EMAIL)
        set_secret(db, user_id, "feed_url", box.encrypt(DEMO_FEED_URL, secret_context(user_id, "feed_url")))
        set_secret(db, user_id, "google_token", box.encrypt("{}", secret_context(user_id, "google_token")))
        if not FRESH:  # --fresh starts with nothing synced yet
            sync_runner.run_sync(db, box, user_id, dry_run=False)  # an initial manual sync
    if not FRESH:
        scheduler.sync_all_users(app, trigger="cron")  # and a scheduled one, for the history table

    @app.get("/demo-login")
    def demo_login():
        session.clear()
        session["user_id"] = user_id
        return redirect("/")

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    print(f"Demo server with FAKE data on http://127.0.0.1:{port}/demo-login (Ctrl+C to stop)")
    build_demo_app().run(host="127.0.0.1", port=port)
