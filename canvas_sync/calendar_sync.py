"""Sync Canvas calendar events into a dedicated Google Calendar named "Canvas"."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from .feed import CanvasItem
from .state import SyncState

CALENDAR_NAME = "Canvas"
UID_PROPERTY = "canvasUid"  # hidden tag on each Google event holding its Canvas UID
MAX_DESCRIPTION = 8000


@dataclass
class EventSyncResult:
    calendar_created: bool = False
    created: list[str] = field(default_factory=list)  # titles
    adopted: list[str] = field(default_factory=list)  # already in Google, re-linked by tag
    already_synced: int = 0
    missing_from_feed: list[str] = field(default_factory=list)  # Canvas UIDs


def ensure_calendar(service: Any, state: SyncState) -> bool:
    """Make sure the "Canvas" calendar exists. Returns True if it was just created.

    The state file is the only way to find it again: the calendar.app.created
    scope can't list calendars, so we can't look it up by name.
    """
    if state.calendar_id:
        try:
            service.calendars().get(calendarId=state.calendar_id).execute()
            return False
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise
            # The calendar was deleted in Google, so its events are gone too.
            state.events.clear()

    calendar = service.calendars().insert(
        body={
            "summary": CALENDAR_NAME,
            "description": "Synced from Canvas by Canvas2Google. Edits here may be overwritten.",
        }
    ).execute()
    state.calendar_id = calendar["id"]
    state.save()
    return True


def _is_date_only(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _time_fields(item: CanvasItem) -> tuple[dict, dict]:
    if item.all_day:
        start = item.start
        # iCal and Google both treat an all-day end date as exclusive (the day after).
        end = item.end if _is_date_only(item.end) and item.end > start else start + timedelta(days=1)
        return {"date": start.isoformat()}, {"date": end.isoformat()}
    start = item.local_start
    end = item.local_end if item.local_end and item.local_end > start else start
    return {"dateTime": start.isoformat()}, {"dateTime": end.isoformat()}


def event_body(item: CanvasItem) -> dict:
    start, end = _time_fields(item)
    summary = f"{item.title} ({item.course})" if item.course else item.title
    parts = [p for p in (item.description, f"Canvas: {item.url}" if item.url else "") if p]
    return {
        "summary": summary,
        "description": "\n\n".join(parts)[:MAX_DESCRIPTION],
        "start": start,
        "end": end,
        "extendedProperties": {"private": {UID_PROPERTY: item.uid}},
    }


def _find_by_uid(service: Any, calendar_id: str, uid: str) -> str | None:
    """Look for an event we created earlier for this UID (e.g. if the state file was lost)."""
    response = service.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"{UID_PROPERTY}={uid}",
        maxResults=1,
    ).execute()
    found = response.get("items", [])
    return found[0]["id"] if found else None


def sync_events(service: Any, items: list[CanvasItem], state: SyncState) -> EventSyncResult:
    """Create Google events for Canvas events not synced yet. Never creates duplicates."""
    result = EventSyncResult(calendar_created=ensure_calendar(service, state))
    events = [i for i in items if i.kind == "event"]

    for item in events:
        if item.uid in state.events:
            result.already_synced += 1
            continue
        existing_id = _find_by_uid(service, state.calendar_id, item.uid)
        if existing_id:
            state.events[item.uid] = existing_id
            result.adopted.append(item.title)
        else:
            created = service.events().insert(
                calendarId=state.calendar_id, body=event_body(item)
            ).execute()
            state.events[item.uid] = created["id"]
            result.created.append(item.title)
        # Save after every change, so a crash or network error mid-run can't make
        # us forget an event we already created.
        state.save()

    feed_uids = {i.uid for i in events}
    result.missing_from_feed = sorted(uid for uid in state.events if uid not in feed_uids)
    return result
