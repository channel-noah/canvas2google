"""Sync Canvas assignments into a Google Tasks list named "Canvas"."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from googleapiclient.errors import HttpError

from .feed import CanvasItem, upcoming
from .google_client import list_all
from .state import SyncState

TASKLIST_NAME = "Canvas"
# Google Tasks has no hidden fields, so the Canvas UID goes on the last line of the
# notes. It lets us re-link tasks if the state file loses track of them.
UID_MARKER = "Canvas ID: "
MAX_NOTES = 8000


@dataclass
class TaskSyncResult:
    tasklist_created: bool = False
    created: list[str] = field(default_factory=list)  # titles
    relinked: list[str] = field(default_factory=list)
    already_synced: int = 0
    skipped_past_due: int = 0
    missing_from_feed: list[str] = field(default_factory=list)  # Canvas UIDs
    possible_duplicates: list[str] = field(default_factory=list)  # override + base pairs


def ensure_tasklist(service: Any, state: SyncState) -> bool:
    """Make sure the "Canvas" task list exists. Returns True if it was just created."""
    if state.tasklist_id:
        try:
            service.tasklists().get(tasklist=state.tasklist_id).execute()
            return False
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise
            state.tasks.clear()  # the list was deleted in Google, and its tasks with it

    for tasklist in list_all(service.tasklists().list):
        if tasklist.get("title") == TASKLIST_NAME:
            state.tasklist_id = tasklist["id"]
            state.save()
            return False

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


def sync_tasks(service: Any, items: list[CanvasItem], state: SyncState) -> TaskSyncResult:
    """Create Google tasks for upcoming Canvas assignments not synced yet."""
    result = TaskSyncResult(tasklist_created=ensure_tasklist(service, state))
    assignments = [i for i in items if i.kind == "assignment"]
    result.possible_duplicates = _possible_duplicates(assignments)

    upcoming_uids = {a.uid for a in upcoming(assignments)}
    to_create = []
    for item in assignments:
        if item.uid in state.tasks:
            result.already_synced += 1
        elif item.uid not in upcoming_uids:
            result.skipped_past_due += 1
        else:
            to_create.append(item)

    # Only read the whole list when there's something to create.
    existing = _tasks_by_uid(service, state.tasklist_id) if to_create else {}
    for item in to_create:
        if item.uid in existing:
            state.tasks[item.uid] = existing[item.uid]
            result.relinked.append(item.title)
        else:
            created = service.tasks().insert(tasklist=state.tasklist_id, body=task_body(item)).execute()
            state.tasks[item.uid] = created["id"]
            result.created.append(item.title)
        state.save()  # after every change, so an error mid-run can't cause duplicates

    feed_uids = {a.uid for a in assignments}
    result.missing_from_feed = sorted(uid for uid in state.tasks if uid not in feed_uids)
    return result
