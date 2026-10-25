"""Test data builders and web app helpers."""

from __future__ import annotations

import re
from datetime import date, datetime

from canvas_sync.feed import CanvasItem

ALLOWED = "noah@example.com"
FEED = "https://school.instructure.com/feeds/calendars/user_SECRETFEED123.ics"


def csrf_token(client, path: str = "/settings") -> str:
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def db_rows(app, sql: str, *args) -> list[tuple]:
    """Run raw SQL against the test database (tests only; app code never does this)."""
    with app.extensions["db_engine"].connect() as conn:
        return [tuple(row) for row in conn.exec_driver_sql(sql, tuple(args))]


def db_file(app):
    from pathlib import Path

    return Path(app.extensions["db_engine"].url.database)


def save_feed_and_token(app, user_id: int) -> None:
    """Give a test user an encrypted feed URL and (fake) Google token."""
    from webapp.crypto import secret_context
    from webapp.db import get_db, set_secret

    box = app.config["SECRET_BOX"]
    with app.app_context():
        db = get_db()
        set_secret(db, user_id, "feed_url", box.encrypt(FEED, secret_context(user_id, "feed_url")))
        set_secret(db, user_id, "google_token", box.encrypt("{}", secret_context(user_id, "google_token")))


def make_item(
    uid: str,
    title: str = "Homework 1",
    kind: str | None = None,
    start: date | datetime = date(2099, 1, 15),
    end: date | datetime | None = None,
    course: str | None = "CS-101-01",
    url: str | None = None,
    description: str = "",
) -> CanvasItem:
    if kind is None:
        kind = "assignment" if uid.startswith("event-assignment-") else "event"
    return CanvasItem(uid, kind, title, course, start, end, url, description)


# A small feed covering the cases seen in the real Canvas feed. Dates are in 2099
# so "upcoming" checks don't depend on today's date.
SAMPLE_FEED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
    # Timed assignment with an HTML description, escaped the way Canvas sends it.
    "BEGIN:VEVENT\r\nUID:event-assignment-1001\r\n"
    "DTSTART:20991030T030000Z\r\nDTEND:20991030T030000Z\r\n"
    "SUMMARY:Homework 3 [MATH-210-01]\r\n"
    "URL:https://school.instructure.com/calendar#assignment_1001\r\n"
    "X-ALT-DESC;FMTTYPE=text/html:<p>Read ch. 4\\, then answer\\; carefully.</p>"
    "<script>alert(1)</script><p>Tom&nbsp\\;&amp\\; Jerry</p>\r\n"
    "END:VEVENT\r\n"
    # All-day assignment (Canvas uses this for 11:59 PM due dates).
    "BEGIN:VEVENT\r\nUID:event-assignment-1002\r\n"
    "DTSTART;VALUE=DATE:20991101\r\nDTEND;VALUE=DATE:20991102\r\n"
    "SUMMARY:Essay draft [ENG-110-02]\r\nEND:VEVENT\r\n"
    # Section override: the title repeats the section name.
    "BEGIN:VEVENT\r\nUID:event-assignment-override-77\r\n"
    "DTSTART:20991012T030000Z\r\nDTEND:20991012T030000Z\r\n"
    "SUMMARY:Ch 4 Homework (MATH-210-01 LINEAR ALGEBRA (2099FA)) [MATH-210-01]\r\n"
    "END:VEVENT\r\n"
    # Calendar event with a terminal escape sequence hidden in the title.
    "BEGIN:VEVENT\r\nUID:event-calendar-event-2002\r\n"
    "DTSTART:20991028T150000Z\r\nDTEND:20991028T161500Z\r\n"
    "SUMMARY:\x1b[31mLab session\x1b[0m [PHYS-201-01]\r\nEND:VEVENT\r\n"
    # Unknown type: must not be synced as anything.
    "BEGIN:VEVENT\r\nUID:event-quiz-5\r\n"
    "DTSTART:20991105T150000Z\r\nSUMMARY:Mystery [MATH-210-01]\r\nEND:VEVENT\r\n"
    # Missing UID: skipped entirely.
    "BEGIN:VEVENT\r\nDTSTART:20991105T150000Z\r\nSUMMARY:No UID\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()
