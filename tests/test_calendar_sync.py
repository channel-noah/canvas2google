import dataclasses
from datetime import date, datetime, timezone

from canvas_sync.calendar_sync import UID_PROPERTY, event_body, sync_events
from fakes import FakeCalendarService
from helpers import make_item

LAB = make_item(
    "event-calendar-event-1", "Lab", course="PHYS-201-01",
    start=datetime(2099, 10, 28, 15, 0, tzinfo=timezone.utc),
    end=datetime(2099, 10, 28, 16, 15, tzinfo=timezone.utc),
    url="https://x.instructure.com/calendar#e", description="Bring goggles",
)
HOLIDAY = make_item("event-calendar-event-2", "Holiday", course=None, start=date(2099, 11, 26))
HOMEWORK = make_item("event-assignment-3", "HW")  # not an event: must be ignored


def test_event_body_shape():
    body = event_body(LAB)
    assert body["summary"] == "Lab (PHYS-201-01)"
    assert body["description"] == "Bring goggles\n\nCanvas: https://x.instructure.com/calendar#e"
    assert body["start"] == {"dateTime": "2099-10-28T15:00:00+00:00"}  # always UTC
    assert body["end"] == {"dateTime": "2099-10-28T16:15:00+00:00"}
    assert body["extendedProperties"]["private"][UID_PROPERTY] == LAB.uid


def test_all_day_event_without_end_lasts_one_day():
    body = event_body(HOLIDAY)
    assert body["start"] == {"date": "2099-11-26"}
    assert body["end"] == {"date": "2099-11-27"}  # Google's all-day end is exclusive


def test_first_run_creates_calendar_and_events_only(state):
    svc = FakeCalendarService()
    result = sync_events(svc, [LAB, HOLIDAY, HOMEWORK], state)

    assert result.calendar_created
    assert sorted(result.created) == ["Holiday", "Lab"]
    assert svc.write_ops().count("events.insert") == 2
    assert state.calendar_id in svc.calendars_by_id


def test_second_run_makes_no_writes(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB, HOLIDAY], state)
    svc.writes.clear()

    result = sync_events(svc, [LAB, HOLIDAY], state)
    assert svc.writes == []
    assert result.unchanged == 2 and not result.created and not result.updated


def test_changed_event_is_patched_not_duplicated(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB], state)
    moved = dataclasses.replace(LAB, start=datetime(2099, 10, 29, 15, 0, tzinfo=timezone.utc),
                                end=datetime(2099, 10, 29, 16, 0, tzinfo=timezone.utc))

    result = sync_events(svc, [moved], state)
    assert result.updated == ["Lab"]
    assert len(svc.events_by_id) == 1
    (event,) = svc.events_by_id.values()
    assert event["start"] == {"dateTime": "2099-10-29T15:00:00+00:00"}


def test_dry_run_writes_nothing_and_saves_nothing(state):
    svc = FakeCalendarService()
    result = sync_events(svc, [LAB, HOLIDAY], state, dry_run=True)

    assert result.calendar_created and sorted(result.created) == ["Holiday", "Lab"]
    assert svc.writes == []
    assert not state.path.exists()


def test_lost_state_entry_is_relinked_by_uid_tag(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB], state)
    state.events.clear()  # simulate a lost entry

    result = sync_events(svc, [LAB], state)
    assert result.relinked == ["Lab"] and not result.created
    assert len(svc.events_by_id) == 1


def test_event_deleted_in_google_is_recreated_when_it_changes(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB], state)
    svc.events_by_id.clear()  # user deleted it in Google

    sync_events(svc, [dataclasses.replace(LAB, title="Lab (moved)")], state)
    assert len(svc.events_by_id) == 1
    assert "events.insert" in svc.write_ops()


def test_deleted_calendar_is_recreated(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB], state)
    svc.calendars_by_id.clear()
    svc.events_by_id.clear()

    result = sync_events(svc, [LAB], state)
    assert result.calendar_created and result.created == ["Lab"]


def test_events_missing_from_feed_are_reported_not_deleted(state):
    svc = FakeCalendarService()
    sync_events(svc, [LAB, HOLIDAY], state)
    svc.writes.clear()

    result = sync_events(svc, [LAB], state)
    assert result.missing_from_feed == [HOLIDAY.uid]
    assert svc.writes == []
