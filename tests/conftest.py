"""Shared test setup.

Every test runs with the network blocked, so no test can ever reach Canvas or a
real Google account, even by mistake.
"""

from __future__ import annotations

import socket

import pytest

from canvas_sync.state import SyncState


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def state(tmp_path) -> SyncState:
    """A fresh state that saves to a temporary file, never the real sync_state.json."""
    return SyncState(path=tmp_path / "sync_state.json")
