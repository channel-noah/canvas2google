"""Sign-in logic that can be tested without a browser or network."""

import json

import pytest

from canvas_sync import google_client as gc
from canvas_sync.google_client import AuthError, get_credentials, list_all


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Never touch the real token.json or credentials.json."""
    monkeypatch.setattr(gc, "TOKEN_FILE", tmp_path / "token.json")
    monkeypatch.setattr(gc, "CREDENTIALS_FILE", tmp_path / "credentials.json")


def write_token(path, scopes, expiry="2099-01-01T00:00:00Z"):
    path.write_text(json.dumps({
        "token": "fake-access", "refresh_token": "fake-refresh",
        "client_id": "id.apps.googleusercontent.com", "client_secret": "fake-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes, "expiry": expiry,
    }), encoding="utf-8")


def test_valid_saved_token_is_reused_without_browser():
    write_token(gc.TOKEN_FILE, gc.SCOPES)
    creds = get_credentials(allow_browser=False)
    assert creds.valid and set(creds.scopes) == set(gc.SCOPES)


def test_token_missing_a_scope_is_not_trusted():
    write_token(gc.TOKEN_FILE, gc.SCOPES[:1])  # only the calendar scope
    with pytest.raises(AuthError, match="Not signed in"):
        get_credentials(allow_browser=False)


def test_unattended_run_without_token_fails_cleanly():
    with pytest.raises(AuthError, match="Not signed in"):
        get_credentials(allow_browser=False)


def test_corrupt_token_file_is_ignored_not_crashed_on():
    gc.TOKEN_FILE.write_text("{garbage", encoding="utf-8")
    with pytest.raises(AuthError):
        get_credentials(allow_browser=False)


def test_missing_credentials_json_is_a_clear_error():
    with pytest.raises(AuthError, match="credentials.json not found"):
        get_credentials(allow_browser=True)


def test_list_all_follows_every_page():
    pages = {None: {"items": [1, 2], "nextPageToken": "p2"}, "p2": {"items": [3]}}

    class Request:
        def __init__(self, token):
            self.token = token

        def execute(self):
            return pages[self.token]

    assert list_all(lambda pageToken: Request(pageToken)) == [1, 2, 3]
