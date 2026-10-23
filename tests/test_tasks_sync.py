import dataclasses
from datetime import date, datetime, timezone

import pytest
from googleapiclient.errors import HttpError

from canvas_sync.tasks_sync import MAX_NOTES, UID_MARKER, due_text, sync_tasks, task_body
from fakes import FakeTasksService, http_error
from helpers import make_item

ESSAY = make_item("event-assignment-1", "Essay", start=date(2099, 11, 1), end=date(2099, 11, 2))
QUIZ = make_item("event-assignment-2", "Quiz", start=datetime(2099, 11, 3, 3, 0, tzinfo=timezone.utc))
OLD = make_item("event-assignment-3", "Old HW", start=date(2000, 1, 1))  # past due
LAB = make_item("event-calendar-event-9", "Lab")  # not an assignment: must be ignored


def test_all_day_assignment_is_due_at_1159pm():
    assert due_text(ESSAY) == "Sun Nov 01, 2099 at 11:59 PM"


def test_task_body_shape():
    body = task_body(ESSAY)
    assert body["title"] == "Essay (CS-101-01)"
    assert body["due"] == "2099-11-01T00:00:00.000Z"  # Google Tasks keeps only the date
    assert body["notes"].startswith("Due: Sun Nov 01, 2099 at 11:59 PM\nCourse: CS-101-01")
    assert body["notes"].endswith(f"{UID_MARKER}{ESSAY.uid}")


def test_task_body_never_includes_status():
    # Sending status on update would un-check tasks the user completed.
    assert "status" not in task_body(ESSAY)


def test_huge_description_is_trimmed_but_keeps_uid_marker():
    body = task_body(dataclasses.replace(ESSAY, description="x" * 50_000))
    assert len(body["notes"]) <= MAX_NOTES
    assert body["notes"].endswith(f"{UID_MARKER}{ESSAY.uid}")


def test_first_run_creates_list_and_upcoming_tasks_only(state):
    svc = FakeTasksService()
    result = sync_tasks(svc, [ESSAY, QUIZ, OLD, LAB], state)

    assert result.tasklist_created
    assert sorted(result.created) == ["Essay", "Quiz"]
    assert result.skipped_past_due == 1
    assert svc.write_ops().count("tasks.insert") == 2


def test_existing_canvas_list_is_reused(state):
    svc = FakeTasksService()
    svc.tasklists_by_id["mine"] = {"id": "mine", "title": "Canvas"}
    result = sync_tasks(svc, [ESSAY], state)
    assert not result.tasklist_created and state.tasklist_id == "mine"


def test_second_run_makes_no_writes(state):
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY, QUIZ], state)
    svc.writes.clear()

    result = sync_tasks(svc, [ESSAY, QUIZ], state)
    assert svc.writes == [] and result.unchanged == 2


def test_change_updates_task_and_keeps_it_completed(state):
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY], state)
    task_id = state.tasks[ESSAY.uid]["id"]
    svc.tasks_by_id[task_id]["status"] = "completed"  # user checked it off

    moved = dataclasses.replace(ESSAY, title="Essay (extended)", start=date(2099, 11, 8), end=date(2099, 11, 9))
    result = sync_tasks(svc, [moved], state)

    task = svc.tasks_by_id[task_id]
    assert result.updated == ["Essay (extended)"]
    assert task["due"] == "2099-11-08T00:00:00.000Z"
    assert task["status"] == "completed"
    assert len(svc.tasks_by_id) == 1


def test_dry_run_writes_nothing_and_saves_nothing(state):
    svc = FakeTasksService()
    result = sync_tasks(svc, [ESSAY, QUIZ], state, dry_run=True)
    assert result.tasklist_created and sorted(result.created) == ["Essay", "Quiz"]
    assert svc.writes == []
    assert not state.path.exists()


def test_lost_state_is_relinked_across_pages(state):
    svc = FakeTasksService(page_size=1)  # forces pagination in the lookup
    sync_tasks(svc, [ESSAY, QUIZ], state)
    state.tasks.clear()

    result = sync_tasks(svc, [ESSAY, QUIZ], state)
    assert sorted(result.relinked) == ["Essay", "Quiz"] and not result.created
    assert len(svc.tasks_by_id) == 2


def test_tracked_task_keeps_updating_after_due_date_passes(state):
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY], state)
    now_past = dataclasses.replace(ESSAY, start=date(2000, 1, 1), end=date(2000, 1, 2))

    result = sync_tasks(svc, [now_past], state)
    assert result.updated == ["Essay"] and result.skipped_past_due == 0


def test_task_deleted_in_google_is_recreated_when_it_changes(state):
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY], state)
    svc.tasks_by_id.clear()  # user deleted it in Google

    sync_tasks(svc, [dataclasses.replace(ESSAY, title="Essay v2")], state)
    assert len(svc.tasks_by_id) == 1
    assert "tasks.insert" in svc.write_ops()


def test_deleted_tasklist_is_recreated(state):
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY], state)
    svc.tasklists_by_id.clear()
    svc.tasks_by_id.clear()

    result = sync_tasks(svc, [ESSAY], state)
    assert result.tasklist_created and result.created == ["Essay"]


def test_server_errors_are_not_mistaken_for_deletion(state, monkeypatch):
    # Only 404/410 mean "deleted". A 500 must stop the run, not trigger a re-create.
    svc = FakeTasksService()
    sync_tasks(svc, [ESSAY], state)
    tasks_api = svc.tasks()

    class Failing:
        def patch(self, **kwargs):
            raise http_error(500)

        def __getattr__(self, name):
            return getattr(tasks_api, name)

    monkeypatch.setattr(svc, "tasks", lambda: Failing())
    with pytest.raises(HttpError):
        sync_tasks(svc, [dataclasses.replace(ESSAY, title="Essay v2")], state)
    assert len(svc.tasks_by_id) == 1


def test_override_matching_regular_assignment_is_flagged(state):
    base = make_item("event-assignment-5", "Ch 4 Homework")
    override = make_item("event-assignment-override-6", "Ch 4 Homework")
    result = sync_tasks(FakeTasksService(), [base, override], state)
    assert result.possible_duplicates == ["Ch 4 Homework"]
