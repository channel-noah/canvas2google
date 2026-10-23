"""Sync Canvas calendar events into a dedicated Google Calendar named "Canvas"."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from googleapiclient.errors import HttpError

from .feed import CanvasItem
from .state import SyncState, fingerprint

CALENDAR_NAME = "Canvas"
UID_PROPERTY = "canvasUid"  # hidden tag on each Google event holding its Canvas UID
MAX_DESCRIPTION = 8000
GONE = (404, 410)


@dataclass
class EventSyncResult:
    dry_run: bool = False
    calendar_created: bool = False
    created: list[str] = field(default_factory=list)  # titles
    updated: list[str] = field(default_factory=list)
    relinked: list[str] = field(default_factory=list)  # already in Google, re-linked by tag
    unchanged: int = 0
    missing_from_feed: list[str] = field(default_factory=list)  # Canvas UIDs


def ensure_calendar(service: Any, state: SyncState, dry_run: bool) -> bool:
    """Make sure the "Canvas" calendar exists. Returns True if it was (or would be) created.

    The state file is the only way to find it again: the calendar.app.created
    scope can't list calendars, so we can't look it up by name.
    """
    if state.calendar_id:
        try:
            service.calendars().get(calendarId=state.calendar_id).execute()
            return False
        except HttpError as exc:
            if exc.resp.status not in GONE:
                raise
            # The calendar was deleted in Google, so its events are gone too.
            state.calendar_id = None
            state.events.clear()

    if dry_run:
        return True
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
    # Send UTC so the request (and its fingerprint) is the same on any machine,
    # whatever its time zone. Google shows it in the calendar's own time zone.
    return (
        {"dateTime": start.astimezone(timezone.utc).isoformat()},
        {"dateTime": end.astimezone(timezone.utc).isoformat()},
    )


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


def _create(service: Any, calendar_id: str, body: dict) -> str:
    return service.events().insert(calendarId=calendar_id, body=body).execute()["id"]


def _update(service: Any, calendar_id: str, event_id: str, body: dict) -> str:
    """Patch an event; if it was deleted in Google, create it again. Returns its ID."""
    try:
        service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
        return event_id
    except HttpError as exc:
        if exc.resp.status not in GONE:
            raise
        return _create(service, calendar_id, body)


def sync_events(
    service: Any, items: list[CanvasItem], state: SyncState, dry_run: bool = False
) -> EventSyncResult:
    """Create new events and update changed ones. Never creates duplicates.

    With dry_run=True, only reads from Google and never saves the state file.
    """
    result = EventSyncResult(dry_run=dry_run, calendar_created=ensure_calendar(service, state, dry_run))
    events = [i for i in items if i.kind == "event"]

    for item in events:
        body = event_body(item)
        new_hash = fingerprint(body)
        entry = state.events.get(item.uid)

        if entry and entry["hash"] == new_hash:
            result.unchanged += 1
            continue

        if entry:
            result.updated.append(item.title)
            event_id = entry["id"]
            if not dry_run:
                event_id = _update(service, state.calendar_id, event_id, body)
        else:
            existing_id = _find_by_uid(service, state.calendar_id, item.uid) if state.calendar_id else None
            if existing_id:
                # Found by its tag. Patch it too, since we don't know what it contains.
                result.relinked.append(item.title)
                event_id = existing_id
                if not dry_run:
                    event_id = _update(service, state.calendar_id, existing_id, body)
            else:
                result.created.append(item.title)
                event_id = None if dry_run else _create(service, state.calendar_id, body)

        if not dry_run:
            state.events[item.uid] = {"id": event_id, "hash": new_hash}
            # Save after every change, so an error mid-run can't make us forget
            # an event we already created.
            state.save()

    feed_uids = {i.uid for i in events}
    result.missing_from_feed = sorted(uid for uid in state.events if uid not in feed_uids)
    return result
