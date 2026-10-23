"""Command-line front end. The real logic lives in canvas_sync/; this file only
handles arguments and printing, so a web front end could reuse the same core later."""

import argparse
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from canvas_sync.config import ConfigError, load_feed_url
from canvas_sync.feed import CanvasItem, FeedError, fetch_feed, parse_feed, upcoming


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


def main(argv: list[str] | None = None) -> int:
    # Never crash on an emoji in a course title, whatever the console encoding.
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Show upcoming Canvas assignments and events.")
    parser.add_argument("--all", action="store_true", help="include past items too")
    parser.add_argument(
        "--inspect", action="store_true", help="summarize UID/URL patterns used to classify items"
    )
    args = parser.parse_args(argv)

    try:
        items = parse_feed(fetch_feed(load_feed_url()))
    except (ConfigError, FeedError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.inspect:
        print_inspection(items)
    elif args.all:
        print_table(sorted(items, key=lambda i: i.local_start))
    else:
        print_table(upcoming(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
