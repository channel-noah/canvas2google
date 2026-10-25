"""Local record of what has been synced.

For each Canvas UID we keep the Google ID and a fingerprint (hash) of what we last
sent to Google. Re-running is safe because recorded items are never created again,
and an item is only updated when its fingerprint changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT, restrict_permissions, write_private_file

STATE_FILE = PROJECT_ROOT / "sync_state.json"
STATE_VERSION = 2


class StateError(Exception):
    """The state file exists but can't be used."""


def fingerprint(body: dict) -> str:
    """A short hash of a Google request body. Same content -> same fingerprint."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _migrate(data: dict) -> dict:
    """Upgrade older state files in place of making the user start over."""
    if data.get("version") == 1:
        # v1 stored just "UID -> Google ID". With no fingerprint, each item gets
        # updated once on the next run, which is harmless.
        for key in ("events", "tasks"):
            data[key] = {uid: {"id": gid, "hash": None} for uid, gid in data.get(key, {}).items()}
        data["version"] = 2
    return data


@dataclass
class SyncState:
    """Sync records for one user.

    The CLI saves to sync_state.json (`path`). The web app passes a `saver`
    function instead, which stores the same data in its database.
    """

    calendar_id: str | None = None
    events: dict[str, dict] = field(default_factory=dict)  # UID -> {"id", "hash"}
    tasklist_id: str | None = None
    tasks: dict[str, dict] = field(default_factory=dict)  # UID -> {"id", "hash"}
    path: Path | None = STATE_FILE
    saver: Callable[[dict], None] | None = None

    @classmethod
    def from_dict(cls, data: object, **where: Any) -> SyncState:
        """Build a state from saved data. `where` is path= or saver=."""
        if not isinstance(data, dict):
            raise StateError("Saved sync state is not in the expected format.")
        data = _migrate(data)
        if data.get("version") != STATE_VERSION:
            raise StateError("Saved sync state has an unknown format version.")
        return cls(
            calendar_id=data.get("calendar_id"),
            events=dict(data.get("events", {})),
            tasklist_id=data.get("tasklist_id"),
            tasks=dict(data.get("tasks", {})),
            **where,
        )

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
        return cls.from_dict(data, path=path)

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "calendar_id": self.calendar_id,
            "events": self.events,
            "tasklist_id": self.tasklist_id,
            "tasks": self.tasks,
        }

    def save(self) -> None:
        if self.saver:
            self.saver(self.to_dict())
        else:
            write_private_file(self.path, json.dumps(self.to_dict(), indent=2, sort_keys=True))
