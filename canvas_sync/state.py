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
    calendar_id: str | None = None
    events: dict[str, dict] = field(default_factory=dict)  # UID -> {"id", "hash"}
    tasklist_id: str | None = None
    tasks: dict[str, dict] = field(default_factory=dict)  # UID -> {"id", "hash"}
    path: Path = STATE_FILE

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> SyncState:
        if not path.exists():
            return cls(path=path)
        restrict_permissions(path)
        try:
            data = _migrate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, AttributeError):
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
