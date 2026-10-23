"""In-memory stand-ins for the Google Calendar and Tasks API clients.

They mimic the googleapiclient call style (service.events().insert(...).execute())
and record every write, so tests can check exactly what would be sent to Google.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable

import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"")


class _Request:
    def __init__(self, run: Callable[[], Any]):
        self._run = run

    def execute(self) -> Any:
        return self._run()


class _Fake:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []  # (operation, id)
        self._ids = itertools.count(1)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def write_ops(self) -> list[str]:
        return [op for op, _ in self.writes]


class FakeCalendarService(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.calendars_by_id: dict[str, dict] = {}
        self.events_by_id: dict[str, dict] = {}

    def calendars(self) -> Any:
        svc = self

        class Calendars:
            def get(self, calendarId):
                def run():
                    if calendarId not in svc.calendars_by_id:
                        raise http_error(404)
                    return svc.calendars_by_id[calendarId]
                return _Request(run)

            def insert(self, body):
                def run():
                    cid = svc._new_id("cal")
                    svc.calendars_by_id[cid] = {**body, "id": cid}
                    svc.writes.append(("calendars.insert", cid))
                    return svc.calendars_by_id[cid]
                return _Request(run)

        return Calendars()

    def events(self) -> Any:
        svc = self

        class Events:
            def insert(self, calendarId, body):
                def run():
                    eid = svc._new_id("evt")
                    svc.events_by_id[eid] = {**body, "id": eid, "calendarId": calendarId}
                    svc.writes.append(("events.insert", eid))
                    return svc.events_by_id[eid]
                return _Request(run)

            def patch(self, calendarId, eventId, body):
                def run():
                    if eventId not in svc.events_by_id:
                        raise http_error(404)
                    svc.events_by_id[eventId].update(body)  # patch = merge
                    svc.writes.append(("events.patch", eventId))
                    return svc.events_by_id[eventId]
                return _Request(run)

            def list(self, calendarId, privateExtendedProperty=None, maxResults=None, pageToken=None):
                def run():
                    key, _, value = (privateExtendedProperty or "").partition("=")
                    found = [
                        e for e in svc.events_by_id.values()
                        if e["calendarId"] == calendarId
                        and e.get("extendedProperties", {}).get("private", {}).get(key) == value
                    ]
                    return {"items": found[:maxResults] if maxResults else found}
                return _Request(run)

        return Events()


class FakeTasksService(_Fake):
    def __init__(self, page_size: int = 2) -> None:
        super().__init__()
        self.tasklists_by_id: dict[str, dict] = {}
        self.tasks_by_id: dict[str, dict] = {}
        self.page_size = page_size  # small pages, to exercise pagination

    def _page(self, items: list[dict], page_token: str | None) -> dict:
        start = int(page_token or 0)
        response = {"items": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            response["nextPageToken"] = str(start + self.page_size)
        return response

    def tasklists(self) -> Any:
        svc = self

        class TaskLists:
            def get(self, tasklist):
                def run():
                    if tasklist not in svc.tasklists_by_id:
                        raise http_error(404)
                    return svc.tasklists_by_id[tasklist]
                return _Request(run)

            def list(self, pageToken=None):
                return _Request(lambda: svc._page(list(svc.tasklists_by_id.values()), pageToken))

            def insert(self, body):
                def run():
                    lid = svc._new_id("list")
                    svc.tasklists_by_id[lid] = {**body, "id": lid}
                    svc.writes.append(("tasklists.insert", lid))
                    return svc.tasklists_by_id[lid]
                return _Request(run)

        return TaskLists()

    def tasks(self) -> Any:
        svc = self

        class Tasks:
            def insert(self, tasklist, body):
                def run():
                    tid = svc._new_id("task")
                    svc.tasks_by_id[tid] = {**body, "id": tid, "tasklist": tasklist, "status": "needsAction"}
                    svc.writes.append(("tasks.insert", tid))
                    return svc.tasks_by_id[tid]
                return _Request(run)

            def patch(self, tasklist, task, body):
                def run():
                    if task not in svc.tasks_by_id:
                        raise http_error(404)
                    svc.tasks_by_id[task].update(body)  # patch = merge
                    svc.writes.append(("tasks.patch", task))
                    return svc.tasks_by_id[task]
                return _Request(run)

            def list(self, tasklist, showCompleted=None, showHidden=None, maxResults=None, pageToken=None):
                items = [t for t in svc.tasks_by_id.values() if t["tasklist"] == tasklist]
                return _Request(lambda: svc._page(items, pageToken))

        return Tasks()
