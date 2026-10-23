"""Google sign-in (OAuth) and API clients. Tokens are stored locally and never printed."""

from __future__ import annotations

import functools
from typing import Any, Callable

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import PROJECT_ROOT, restrict_permissions, write_private_file

# Least privilege:
# - calendar.app.created: create calendars, and manage events only on calendars this
#   app created. It can't read or change any of your other calendars.
# - tasks: Google has no narrower Tasks scope that can create task lists and tasks.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/tasks",
]

CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"  # identifies this app to Google
TOKEN_FILE = PROJECT_ROOT / "token.json"  # your sign-in; acts like a password

HTTP_TIMEOUT = 30  # seconds, for every Google API request
SIGN_IN_TIMEOUT = 300  # seconds to finish signing in in the browser


class AuthError(Exception):
    """Sign-in failed. Messages never include tokens or client secrets."""


def _save_token(creds: Credentials) -> None:
    write_private_file(TOKEN_FILE, creds.to_json())


def _load_saved_token() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    restrict_permissions(TOKEN_FILE)
    try:
        # No scopes argument: we want the scopes actually saved with the token,
        # not the ones we'd like to have.
        return Credentials.from_authorized_user_file(str(TOKEN_FILE))
    except (ValueError, OSError):
        return None  # unreadable or corrupt; sign in again


def get_credentials(allow_browser: bool = True) -> Credentials:
    """Return valid Google credentials, refreshing or signing in as needed.

    allow_browser=False is for unattended runs (like cron), where nobody can sign in.
    """
    creds = _load_saved_token()
    if creds and not creds.has_scopes(SCOPES):
        creds = None  # the app now needs different permissions; ask again

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except (RefreshError, GoogleAuthError):
            creds = None  # access was revoked or expired; sign in again

    if not allow_browser:
        raise AuthError("Not signed in to Google. Run `python cli.py --check-google` to sign in.")
    if not CREDENTIALS_FILE.exists():
        raise AuthError("credentials.json not found in the project folder. See the Milestone 2 setup steps.")

    restrict_permissions(CREDENTIALS_FILE)
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        # The library's code-for-token exchange has no timeout and run_local_server
        # can't pass one through, so give that one request a default timeout.
        flow.oauth2session.fetch_token = functools.partial(
            flow.oauth2session.fetch_token, timeout=HTTP_TIMEOUT
        )
        creds = flow.run_local_server(
            host="localhost",
            port=0,  # any free port
            authorization_prompt_message="Opening your browser to sign in to Google...",
            success_message="Signed in. You can close this tab and return to the terminal.",
            timeout_seconds=SIGN_IN_TIMEOUT,
        )
    except Exception as exc:
        # Don't echo the underlying message: OAuth errors can include request details.
        raise AuthError(f"Google sign-in didn't complete ({type(exc).__name__}).") from None

    # Google's consent screen lets you untick individual permissions.
    granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    if not set(SCOPES) <= granted:
        raise AuthError("Both Calendar and Tasks access are needed. Run again and allow both.")

    _save_token(creds)
    return creds


def build_services(creds: Credentials) -> tuple[Any, Any]:
    """Return (calendar, tasks) API clients with a request timeout."""

    def authorized_http() -> google_auth_httplib2.AuthorizedHttp:
        return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))

    calendar = build("calendar", "v3", http=authorized_http(), cache_discovery=False)
    tasks = build("tasks", "v1", http=authorized_http(), cache_discovery=False)
    return calendar, tasks


def list_all(method: Callable[..., Any], **kwargs: Any) -> list[dict]:
    """Call a Google "list" method and follow nextPageToken until every page is read."""
    items: list[dict] = []
    page_token = None
    while True:
        response = method(pageToken=page_token, **kwargs).execute()
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items
