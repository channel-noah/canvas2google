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


# --- Web app fixtures (temp database, throwaway keys) ----------------------

@pytest.fixture
def app(tmp_path):
    from cryptography.fernet import Fernet

    from helpers import ALLOWED
    from webapp import create_app
    from webapp.settings import Settings

    settings = Settings(
        secret_key="test-secret-key",
        encryption_key=Fernet.generate_key().decode(),
        google_client_id="client-id.apps.googleusercontent.com",
        google_client_secret="client-secret",
        allowed_emails=frozenset({ALLOWED}),
        database_url="sqlite:///" + (tmp_path / "test.db").as_posix(),
        cron_secret="c" * 40,
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user_id(app):
    from helpers import ALLOWED
    from webapp.db import get_db, upsert_user

    with app.app_context():
        return upsert_user(get_db(), "google-sub-1", ALLOWED)


@pytest.fixture
def logged_in(client, user_id):
    with client.session_transaction() as s:
        s["user_id"] = user_id
    return client
