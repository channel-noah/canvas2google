import json

import pytest

from canvas_sync.state import STATE_VERSION, StateError, SyncState, fingerprint


def test_missing_file_gives_empty_state(tmp_path):
    s = SyncState.load(tmp_path / "nope.json")
    assert s.calendar_id is None and s.events == {} and s.tasks == {}


def test_save_and_load_round_trip(state):
    state.calendar_id = "cal1"
    state.tasklist_id = "list1"
    state.events["uid-e"] = {"id": "evt1", "hash": "abc"}
    state.tasks["uid-t"] = {"id": "task1", "hash": "def"}
    state.save()

    loaded = SyncState.load(state.path)
    assert (loaded.calendar_id, loaded.tasklist_id) == ("cal1", "list1")
    assert loaded.events == state.events and loaded.tasks == state.tasks


def test_version_1_file_is_migrated(tmp_path):
    path = tmp_path / "sync_state.json"
    path.write_text(json.dumps({
        "version": 1, "calendar_id": "cal1", "events": {"uid-e": "evt1"},
        "tasklist_id": "list1", "tasks": {"uid-t": "task1"},
    }), encoding="utf-8")

    loaded = SyncState.load(path)
    assert loaded.events == {"uid-e": {"id": "evt1", "hash": None}}
    assert loaded.tasks == {"uid-t": {"id": "task1", "hash": None}}
    loaded.save()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == STATE_VERSION


@pytest.mark.parametrize("content", ["{not json", '{"version": 99}', "[1, 2, 3]"])
def test_unusable_file_stops_instead_of_starting_over(tmp_path, content):
    # Silently starting from empty state would re-create everything as duplicates.
    path = tmp_path / "sync_state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError):
        SyncState.load(path)


def test_fingerprint_is_stable_and_order_independent():
    a = {"title": "HW", "due": "2099-01-01", "notes": "n"}
    b = {"notes": "n", "due": "2099-01-01", "title": "HW"}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint({**a, "title": "HW 2"})
