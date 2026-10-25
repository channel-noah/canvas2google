from datetime import date, datetime, timezone

import pytest
import requests

from canvas_sync import feed
from canvas_sync.feed import FeedError, classify, parse_feed, upcoming
from helpers import SAMPLE_FEED, make_item

SECRET_URL = "https://school.instructure.com/feeds/calendars/user_SECRETTOKEN123.ics"


@pytest.fixture
def items():
    return {i.uid: i for i in parse_feed(SAMPLE_FEED)}


# --- Classification -------------------------------------------------------

@pytest.mark.parametrize(
    "uid, kind",
    [
        ("event-assignment-1001", "assignment"),
        ("event-assignment-override-77", "assignment"),
        ("event-calendar-event-2002", "event"),
        ("event-quiz-5", "unknown"),
        ("event-assignment-abc", "unknown"),
        ("xevent-assignment-1", "unknown"),
        ("event-calendar-event-3 extra", "unknown"),
    ],
)
def test_classify_is_strict_about_uid_format(uid, kind):
    assert classify(uid) == kind


# --- Parsing --------------------------------------------------------------

def test_items_without_uid_are_skipped(items):
    assert len(items) == 5
    assert all(i.title != "No UID" for i in items.values())


def test_course_is_split_from_title(items):
    hw = items["event-assignment-1001"]
    assert (hw.title, hw.course) == ("Homework 3", "CS-170-01")


def test_override_title_drops_repeated_section_name(items):
    assert items["event-assignment-override-77"].title == "Ch 4 Homework"


def test_html_description_is_unescaped_and_cleaned(items):
    desc = items["event-assignment-1001"].description
    assert desc == "Read ch. 4, then answer; carefully.\n\nTom & Jerry"
    assert "alert" not in desc


def test_control_characters_removed_from_titles(items):
    assert "\x1b" not in items["event-calendar-event-2002"].title


def test_all_day_items_keep_date_values(items):
    essay = items["event-assignment-1002"]
    assert essay.all_day
    assert essay.start == date(2099, 11, 1)


def test_invalid_ical_raises_feed_error():
    with pytest.raises(FeedError):
        parse_feed(b"this is not a calendar")


# --- upcoming() -----------------------------------------------------------

def test_upcoming_filters_past_items_and_sorts_soonest_first():
    now = datetime(2099, 1, 10, 12, 0, tzinfo=timezone.utc)
    past = make_item("event-assignment-1", "Past", start=date(2099, 1, 9))
    today = make_item("event-assignment-2", "Today", start=date(2099, 1, 10))
    later = make_item("event-assignment-3", "Later", start=datetime(2099, 1, 12, tzinfo=timezone.utc))
    assert [i.title for i in upcoming([later, past, today], now=now)] == ["Today", "Later"]


# --- fetch_feed(): HTTPS, limits, and never leaking the URL ---------------

class FakeResponse:
    def __init__(self, url, status=200, chunks=(b"data",)):
        self.url, self.status_code, self._chunks = url, status, chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        return iter(self._chunks)


def test_fetch_uses_timeouts_and_never_follows_redirects(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(url)

    monkeypatch.setattr(requests, "get", fake_get)
    assert feed.fetch_feed(SECRET_URL) == b"data"
    assert seen["timeout"] == (feed.CONNECT_TIMEOUT, feed.READ_TIMEOUT)
    assert seen["allow_redirects"] is False


def test_network_error_message_does_not_leak_url(monkeypatch):
    def fake_get(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(FeedError) as err:
        feed.fetch_feed(SECRET_URL)
    assert "SECRETTOKEN" not in str(err.value)
    # The original exception (which contains the URL) must not be chained into tracebacks.
    assert err.value.__suppress_context__ and err.value.__cause__ is None


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused(monkeypatch, status):
    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse(url, status=status))
    with pytest.raises(FeedError, match="redirected"):
        feed.fetch_feed(SECRET_URL)


def test_http_error_status_is_reported_without_url(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse(url, status=403))
    with pytest.raises(FeedError) as err:
        feed.fetch_feed(SECRET_URL)
    assert "403" in str(err.value) and "SECRETTOKEN" not in str(err.value)


def test_oversized_feed_is_refused(monkeypatch):
    big = [b"x" * (1024 * 1024)] * 11  # 11 MB
    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse(url, chunks=big))
    with pytest.raises(FeedError, match="10 MB"):
        feed.fetch_feed(SECRET_URL)
