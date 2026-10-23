"""Command-line front end. The real logic lives in canvas_sync/; this file only
handles arguments and printing, so a web front end could reuse the same core later."""

import argparse
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from canvas_sync.calendar_sync import EventSyncResult, sync_events
from canvas_sync.config import ConfigError, load_feed_url
from canvas_sync.feed import CanvasItem, FeedError, fetch_feed, parse_feed, upcoming
from canvas_sync.google_client import AuthError, build_services, get_credentials, list_all
from canvas_sync.state import StateError, SyncState
from canvas_sync.tasks_sync import TaskSyncResult, sync_tasks


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _when(item: CanvasItem) -> str:
    if item.all_day:
        return item.local_start.strftime("%a %b %d") + " (all day)"
    start = item.local_start
    text = start.strftime("%a %b %d %I:%M %p")
    end = item.local_end
    if item.kind == "event" and end and end != start:
        text += end.strftime(" - %I:%M %p")
    return text


def print_table(items: list[CanvasItem]) -> None:
    if not items:
        print("No upcoming items in the feed.")
        return
    header = f"{'TYPE':<10}  {'DUE / WHEN':<30}  {'COURSE':<24}  TITLE"
    print(header)
    print("-" * len(header) + "-" * 40)
    for item in items:
        print(
            f"{item.kind:<10}  {_when(item):<30}  "
            f"{_truncate(item.course or '-', 24):<24}  {_truncate(item.title, 60)}"
        )
    counts = Counter(i.kind for i in items)
    print(f"\n{counts['assignment']} assignments, {counts['event']} events, {counts['unknown']} unrecognized")
    if counts["unknown"]:
        print(
            f"Warning: {counts['unknown']} item(s) have an unrecognized Canvas UID format and "
            "won't be synced. Run with --inspect to see them.",
            file=sys.stderr,
        )


def _pattern(text: str) -> str:
    """Replace numbers with N so IDs that differ only by number group together."""
    return re.sub(r"\d+", "N", text)


def print_inspection(items: list[CanvasItem]) -> None:
    """Show the UID and URL shapes in the feed, to check how items get classified."""
    uid_groups: dict[str, list[CanvasItem]] = defaultdict(list)
    url_groups: dict[str, list[CanvasItem]] = defaultdict(list)
    for item in items:
        uid_groups[_pattern(item.uid)].append(item)
        if item.url:
            # Only the path/query/fragment; the school's hostname isn't needed here.
            parts = urlsplit(item.url)
            shape = parts.path + (f"?{parts.query}" if parts.query else "") + (
                f"#{parts.fragment}" if parts.fragment else ""
            )
            url_groups[_pattern(shape)].append(item)
        else:
            url_groups["(no URL)"].append(item)

    for label, groups in (("UID patterns", uid_groups), ("Item URL patterns", url_groups)):
        print(f"{label}:")
        for shape, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            kinds = ", ".join(f"{k}={n}" for k, n in Counter(i.kind for i in group).items())
            all_day = sum(i.all_day for i in group)
            same_start_end = sum(1 for i in group if i.end is not None and i.end == i.start)
            print(f"  {len(group):>4}  {shape}")
            print(f"        classified as: {kinds}; all-day: {all_day}; start==end: {same_start_end}")
            for example in group[:2]:
                print(f"        e.g. {_truncate(example.title, 60)}")
        print()


def _show_list(label: str, method, name_key: str, forbidden_note: str = "") -> None:
    print(f"{label}:")
    try:
        entries = list_all(method)
    except HttpError as exc:
        print(f"  Google refused this request (HTTP {exc.resp.status}).")
        if exc.resp.status == 403 and forbidden_note:
            print(f"  {forbidden_note}")
        return
    except (OSError, HttpLib2Error, GoogleAuthError) as exc:
        print(f"  Couldn't reach Google ({type(exc).__name__}).")
        return
    if not entries:
        print("  (none)")
    for entry in entries:
        print(f"  - {entry.get(name_key, '(untitled)')}")


def check_google() -> int:
    """Sign in (if needed) and list what this app can see in Google."""
    try:
        calendar, tasks = build_services(get_credentials())
    except AuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Signed in to Google.\n")
    _show_list(
        "Calendars this app can access",
        calendar.calendarList().list,
        "summary",
        forbidden_note="Expected: the calendar.app.created scope can't list calendars this app didn't create.",
    )
    _show_list("Task lists", tasks.tasklists().list, "title")
    return 0


def _print_missing(kind: str, uids: list[str]) -> None:
    if uids:
        # Left alone on purpose: removing items from Google is out of scope for now.
        print(f"{len(uids)} synced {kind}(s) are no longer in the Canvas feed (left unchanged):")
        for uid in uids:
            print(f"  ? {uid}")


def print_event_result(result: EventSyncResult) -> None:
    print("== Events -> Google Calendar ==")
    if result.calendar_created:
        print('Created the "Canvas" calendar in Google Calendar.')
    for title in result.created:
        print(f"  + created: {_truncate(title, 70)}")
    for title in result.adopted:
        print(f"  = re-linked existing Google event: {_truncate(title, 70)}")
    print(
        f"Events: {len(result.created)} created, {len(result.adopted)} re-linked, "
        f"{result.already_synced} already synced."
    )
    _print_missing("event", result.missing_from_feed)


def print_task_result(result: TaskSyncResult) -> None:
    print("== Assignments -> Google Tasks ==")
    if result.tasklist_created:
        print('Created the "Canvas" task list in Google Tasks.')
    for title in result.created:
        print(f"  + created: {_truncate(title, 70)}")
    for title in result.relinked:
        print(f"  = re-linked existing Google task: {_truncate(title, 70)}")
    print(
        f"Assignments: {len(result.created)} created, {len(result.relinked)} re-linked, "
        f"{result.already_synced} already synced, {result.skipped_past_due} past due (skipped)."
    )
    _print_missing("assignment", result.missing_from_feed)
    for title in result.possible_duplicates:
        print(f"  ! section override may duplicate a regular assignment: {_truncate(title, 60)}")


def sync_command(items: list[CanvasItem], events: bool, tasks: bool) -> int:
    try:
        state = SyncState.load()
        calendar, tasks_service = build_services(get_credentials())
        if events:
            print_event_result(sync_events(calendar, items, state))
        if tasks:
            if events:
                print()
            print_task_result(sync_tasks(tasks_service, items, state))
    except (AuthError, StateError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except HttpError as exc:
        print(f"Error: Google refused a request (HTTP {exc.resp.status}). Progress so far is saved.", file=sys.stderr)
        return 1
    except (OSError, HttpLib2Error, GoogleAuthError) as exc:
        print(f"Error: couldn't reach Google ({type(exc).__name__}). Progress so far is saved.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # Never crash on an emoji in a course title, whatever the console encoding.
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Show upcoming Canvas assignments and events.")
    parser.add_argument("--all", action="store_true", help="include past items too")
    parser.add_argument(
        "--inspect", action="store_true", help="summarize UID/URL patterns used to classify items"
    )
    parser.add_argument(
        "--check-google", action="store_true", help="sign in to Google and list calendars/task lists"
    )
    parser.add_argument(
        "--sync-events", action="store_true", help='sync Canvas events into the "Canvas" Google calendar'
    )
    parser.add_argument(
        "--sync-tasks", action="store_true", help='sync upcoming assignments into the "Canvas" task list'
    )
    args = parser.parse_args(argv)

    if args.check_google:
        return check_google()

    try:
        items = parse_feed(fetch_feed(load_feed_url()))
    except (ConfigError, FeedError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.sync_events or args.sync_tasks:
        return sync_command(items, events=args.sync_events, tasks=args.sync_tasks)
    if args.inspect:
        print_inspection(items)
    elif args.all:
        print_table(sorted(items, key=lambda i: i.local_start))
    else:
        print_table(upcoming(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
