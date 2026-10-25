"""Fetch the Canvas iCal feed and turn it into CanvasItem objects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

import requests
from icalendar import Calendar

from .text import html_to_text, strip_control_chars

CONNECT_TIMEOUT = 5  # seconds to open the connection
READ_TIMEOUT = 30  # seconds to wait for data
MAX_FEED_BYTES = 10 * 1024 * 1024

# Canvas titles look like "Homework 3 [CS 101 Intro to Programming]".
_COURSE_SUFFIX = re.compile(r"^(?P<title>.*?)\s*\[(?P<course>[^\[\]]+)\]\s*$")
_ASSIGNMENT_UID = re.compile(r"^event-assignment-(override-)?\d+$")
_EVENT_UID = re.compile(r"^event-calendar-event-\d+$")


class FeedError(Exception):
    """The feed couldn't be fetched or parsed. Messages never include the feed URL."""


@dataclass(frozen=True)
class CanvasItem:
    uid: str
    kind: str  # "assignment", "event" or "unknown"
    title: str
    course: str | None
    start: date | datetime
    end: date | datetime | None
    url: str | None
    description: str

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    def start_in(self, tz: tzinfo | None = None) -> datetime:
        return to_local(self.start, tz)

    def end_in(self, tz: tzinfo | None = None) -> datetime | None:
        return to_local(self.end, tz) if self.end is not None else None

    @property
    def local_start(self) -> datetime:
        """Start in this computer's time zone."""
        return self.start_in()

    @property
    def local_end(self) -> datetime | None:
        return self.end_in()


def to_local(value: date | datetime, tz: tzinfo | None = None) -> datetime:
    """Convert an iCal date or datetime to a timezone-aware datetime.

    `tz` is the user's time zone; None means this computer's. A server can be in
    a different time zone from its user, so the web app always passes one.
    """
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is None:
        # A naive ("floating") time means wall-clock time wherever the user is.
        return value.replace(tzinfo=tz) if tz else value.astimezone()
    return value.astimezone(tz) if tz else value.astimezone()


def fetch_feed(url: str) -> bytes:
    try:
        # Redirects aren't followed: Canvas feeds don't redirect, and following them
        # would let a URL that passed validation lead the server somewhere else (SSRF).
        with requests.get(
            url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True, allow_redirects=False
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise FeedError("The feed URL redirected somewhere else; refusing to follow it.")
            if resp.status_code != 200:
                raise FeedError(
                    f"Canvas returned HTTP {resp.status_code}. Check that the feed URL is current."
                )
            chunks = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_FEED_BYTES:
                    raise FeedError("The feed is larger than 10 MB; refusing to read it.")
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.Timeout:
        raise FeedError("Timed out contacting Canvas.") from None
    except requests.RequestException as exc:
        # requests puts the full URL (including the feed's secret token) in its error
        # messages, so report only the error type. "from None" also keeps the original
        # exception, and its message, out of any traceback.
        raise FeedError(f"Network error contacting Canvas ({type(exc).__name__}).") from None


def classify(uid: str) -> str:
    """Return "assignment", "event" or "unknown" based on the Canvas UID format.

    Seen in the real feed: "event-assignment-123" and "event-assignment-override-123"
    (an assignment with a section-specific due date). Calendar events use
    "event-calendar-event-123" (not yet seen in the real feed). Anything else is
    "unknown" so it can't silently be synced as the wrong type.
    """
    if _ASSIGNMENT_UID.match(uid):
        return "assignment"
    if _EVENT_UID.match(uid):
        return "event"
    return "unknown"


def _split_course(summary: str) -> tuple[str, str | None]:
    match = _COURSE_SUFFIX.match(summary)
    if not match:
        return summary, None
    title, course = match["title"], match["course"].strip()
    # Section overrides repeat the section name in the title, e.g.
    # "Chapter 4 Homework (CS-170-01 DISCRETE STRUCTURES (2026FA))"; drop that part.
    section = title.rfind(f" ({course}")
    if section > 0 and title.endswith(")"):
        title = title[:section]
    return title, course


_ICAL_ESCAPES = re.compile(r"\\([\\;,nN])")


def _unescape_ical(text: str) -> str:
    """Undo iCal text escaping (\\, \\; \\n \\\\).

    icalendar does this for standard fields like DESCRIPTION, but not for
    nonstandard ones like Canvas's X-ALT-DESC.
    """
    return _ICAL_ESCAPES.sub(lambda m: "\n" if m[1] in "nN" else m[1], text)


def _description(comp) -> str:
    if "X-ALT-DESC" in comp:  # HTML version, if Canvas sent one
        return html_to_text(_unescape_ical(str(comp["X-ALT-DESC"])))
    return html_to_text(str(comp.get("DESCRIPTION", "")))


def parse_feed(data: bytes) -> list[CanvasItem]:
    try:
        cal = Calendar.from_ical(data)
    except Exception:
        raise FeedError("The feed isn't valid iCal data.") from None

    items = []
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID", "")).strip()
        dtstart = comp.get("DTSTART")
        if not uid or dtstart is None:
            continue
        summary = strip_control_chars(str(comp.get("SUMMARY", ""))).strip()
        title, course = _split_course(summary)
        dtend = comp.get("DTEND")
        url = str(comp["URL"]) if "URL" in comp else None
        items.append(
            CanvasItem(
                uid=uid,
                kind=classify(uid),
                title=title,
                course=course,
                start=dtstart.dt,
                end=dtend.dt if dtend is not None else None,
                url=url,
                description=_description(comp),
            )
        )
    return items


def upcoming(
    items: list[CanvasItem], now: datetime | None = None, tz: tzinfo | None = None
) -> list[CanvasItem]:
    """Items that haven't ended yet, soonest first. "Today" is today in `tz`."""
    now = now or (datetime.now(tz) if tz else datetime.now().astimezone())
    today = now.astimezone(tz).date() if tz else now.date()

    def still_relevant(item: CanvasItem) -> bool:
        if item.all_day:
            return item.start >= today
        return (item.end_in(tz) or item.start_in(tz)) >= now

    return sorted((i for i in items if still_relevant(i)), key=lambda i: i.start_in(tz))
