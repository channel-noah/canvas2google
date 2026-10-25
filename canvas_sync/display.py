"""Human-readable formatting shared by the CLI and the web app."""

from .feed import CanvasItem


def when_text(item: CanvasItem) -> str:
    if item.all_day:
        return item.local_start.strftime("%a %b %d") + " (all day)"
    start = item.local_start
    text = start.strftime("%a %b %d %I:%M %p")
    end = item.local_end
    if item.kind == "event" and end and end != start:
        text += end.strftime(" - %I:%M %p")
    return text
