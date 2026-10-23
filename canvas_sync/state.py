"""Local record of what has been synced: Canvas UID -> Google ID.

This file is what makes re-running safe: anything already recorded here is
never created again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT, restrict_permissions, write_private_file

STATE_FILE = PROJECT_ROOT / "sync_state.json"
STATE_VERSION = 1


class StateError(Exception):
    """The state file exists but can't be used."""


@dataclass
class SyncState:
    calendar_id: str | None = None
    events: dict[str, str] = field(default_factory=dict)  # Canvas UID -> Google event ID
    tasklist_id: str | None = None
    tasks: dict[str, str] = field(default_factory=dict)  # Canvas UID -> Google task ID
    path: Path = STATE_FILE

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> SyncState:
        if not path.exists():
            return cls(path=path)
        restrict_permissions(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Don't silently start over: an empty state would re-create everything.
            raise StateError(f"{path.name} is unreadable. Fix or delete it, then run again.") from None
        if data.get("version") != STATE_VERSION:
            raise StateError(f"{path.name} has an unknown format version.")
        return cls(
            calendar_id=data.get("calendar_id"),
            events=dict(data.get("events", {})),
            tasklist_id=data.get("tasklist_id"),
            tasks=dict(data.get("tasks", {})),
            path=path,
        )

    def save(self) -> None:
        data = {
            "version": STATE_VERSION,
            "calendar_id": self.calendar_id,
            "events": self.events,
            "tasklist_id": self.tasklist_id,
            "tasks": self.tasks,
        }
        write_private_file(self.path, json.dumps(data, indent=2, sort_keys=True))
