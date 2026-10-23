"""Sync Canvas assignments into a Google Tasks list named "Canvas"."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from googleapiclient.errors import HttpError

from .feed import CanvasItem, upcoming
from .google_client import list_all
from .state import SyncState, fingerprint

TASKLIST_NAME = "Canvas"
# Google Tasks has no hidden fields, so the Canvas UID goes on the last line of the
# notes. It lets us re-link tasks if the state file loses track of them.
UID_MARKER = "Canvas ID: "
MAX_NOTES = 8000
GONE = (404, 410)


@dataclass
class TaskSyncResult:
    dry_run: bool = False
    tasklist_created: bool = False
    created: list[str] = field(default_factory=list)  # titles
    updated: list[str] = field(default_factory=list)
    relinked: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped_past_due: int = 0
    missing_from_feed: list[str] = field(default_factory=list)  # Canvas UIDs
    possible_duplicates: list[str] = field(default_factory=list)  # override + base pairs


def ensure_tasklist(service: Any, state: SyncState, dry_run: bool) -> bool:
    """Make sure the "Canvas" task list exists. Returns True if it was (or would be) created."""
    if state.tasklist_id:
        try:
            service.tasklists().get(tasklist=state.tasklist_id).execute()
            return False
        except HttpError as exc:
            if exc.resp.status not in GONE:
                raise
            state.tasklist_id = None
            state.tasks.clear()  # the list was deleted in Google, and its tasks with it

    for tasklist in list_all(service.tasklists().list):
        if tasklist.get("title") == TASKLIST_NAME:
            state.tasklist_id = tasklist["id"]
            if not dry_run:
                state.save()
            return False

    if dry_run:
        return True
    tasklist = service.tasklists().insert(body={"title": TASKLIST_NAME}).execute()
    state.tasklist_id = tasklist["id"]
    state.save()
    return True


def due_text(item: CanvasItem) -> str:
    # Canvas sends assignments due at 11:59 PM as all-day items (confirmed by Noah).
    if item.all_day:
        return item.start.strftime("%a %b %d, %Y") + " at 11:59 PM"
    return item.local_start.strftime("%a %b %d, %Y at %I:%M %p")


def task_body(item: CanvasItem) -> dict:
    """The fields we manage. Others (like completed status) are never sent, so
    updating a task can't un-check it."""
    due_date = item.start if item.all_day else item.local_start.date()
    marker = f"{UID_MARKER}{item.uid}"
    header = f"Due: {due_text(item)}" + (f"\nCourse: {item.course}" if item.course else "")
    parts = [p for p in (header, item.description, f"Canvas: {item.url}" if item.url else "") if p]
    # Trim the middle, never the marker, so the UID tag always survives.
    notes = "\n\n".join(parts)[: MAX_NOTES - len(marker) - 2] + "\n\n" + marker
    return {
        "title": f"{item.title} ({item.course})" if item.course else item.title,
        "notes": notes,
        # Google Tasks stores only the date; the time part must be midnight UTC.
        "due": f"{due_date.isoformat()}T00:00:00.000Z",
    }


def _tasks_by_uid(service: Any, tasklist_id: str) -> dict[str, str]:
    """Map Canvas UID -> task ID by reading the marker line in each task's notes."""
    found = {}
    tasks = list_all(
        service.tasks().list, tasklist=tasklist_id, showCompleted=True, showHidden=True, maxResults=100
    )
    for task in tasks:
        for line in task.get("notes", "").splitlines():
            if line.startswith(UID_MARKER):
                found[line[len(UID_MARKER):].strip()] = task["id"]
    return found


def _possible_duplicates(assignments: list[CanvasItem]) -> list[str]:
    """Overrides whose title matches a regular assignment in the same course."""
    base = {(a.course, a.title.lower()) for a in assignments if "override" not in a.uid}
    return [
        a.title for a in assignments
        if "override" in a.uid and (a.course, a.title.lower()) in base
    ]


def _create(service: Any, tasklist_id: str, body: dict) -> str:
    return service.tasks().insert(tasklist=tasklist_id, body=body).execute()["id"]


def _update(service: Any, tasklist_id: str, task_id: str, body: dict) -> str:
    """Patch a task; if it was deleted in Google, create it again. Returns its ID."""
    try:
        service.tasks().patch(tasklist=tasklist_id, task=task_id, body=body).execute()
        return task_id
    except HttpError as exc:
        if exc.resp.status not in GONE:
            raise
        return _create(service, tasklist_id, body)


def sync_tasks(
    service: Any, items: list[CanvasItem], state: SyncState, dry_run: bool = False
) -> TaskSyncResult:
    """Create tasks for new upcoming assignments and update changed ones.

    Past-due assignments are only skipped if they were never synced; once
    tracked, a task keeps getting updates. With dry_run=True, only reads from
    Google and never saves the state file.
    """
    result = TaskSyncResult(dry_run=dry_run, tasklist_created=ensure_tasklist(service, state, dry_run))
    assignments = [i for i in items if i.kind == "assignment"]
    result.possible_duplicates = _possible_duplicates(assignments)
    upcoming_uids = {a.uid for a in upcoming(assignments)}

    new_items = [a for a in assignments if a.uid not in state.tasks and a.uid in upcoming_uids]
    result.skipped_past_due = sum(
        1 for a in assignments if a.uid not in state.tasks and a.uid not in upcoming_uids
    )
    # Only read the whole list when there's something new to place.
    existing = _tasks_by_uid(service, state.tasklist_id) if new_items and state.tasklist_id else {}

    for item in assignments:
        if item.uid not in state.tasks and item.uid not in upcoming_uids:
            continue  # never synced and already past due
        body = task_body(item)
        new_hash = fingerprint(body)
        entry = state.tasks.get(item.uid)

        if entry and entry["hash"] == new_hash:
            result.unchanged += 1
            continue

        if entry:
            result.updated.append(item.title)
            task_id = entry["id"]
            if not dry_run:
                task_id = _update(service, state.tasklist_id, task_id, body)
        elif item.uid in existing:
            # Found by its marker. Patch it too, since we don't know what it contains.
            result.relinked.append(item.title)
            task_id = existing[item.uid]
            if not dry_run:
                task_id = _update(service, state.tasklist_id, task_id, body)
        else:
            result.created.append(item.title)
            task_id = None if dry_run else _create(service, state.tasklist_id, body)

        if not dry_run:
            state.tasks[item.uid] = {"id": task_id, "hash": new_hash}
            state.save()  # after every change, so an error mid-run can't cause duplicates

    feed_uids = {a.uid for a in assignments}
    result.missing_from_feed = sorted(uid for uid in state.tasks if uid not in feed_uids)
    return result
