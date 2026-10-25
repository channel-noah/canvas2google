"""Human-readable formatting shared by the CLI and the web app."""

from datetime import tzinfo

from .feed import CanvasItem


def when_text(item: CanvasItem, tz: tzinfo | None = None) -> str:
    """When an item is due or happening, in `tz` (None = this computer's time zone)."""
    if item.all_day:
        return item.start_in(tz).strftime("%a %b %d") + " (all day)"
    start = item.start_in(tz)
    text = start.strftime("%a %b %d %I:%M %p")
    end = item.end_in(tz)
    if item.kind == "event" and end and end != start:
        text += end.strftime(" - %I:%M %p")
    return text
