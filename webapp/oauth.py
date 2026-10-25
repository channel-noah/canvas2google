"""Google OAuth for the web app: one sign-in that both identifies the user and
grants the Calendar/Tasks access used for syncing."""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from canvas_sync import google_client

from .settings import Settings

# Google may grant fewer scopes than requested (users can untick boxes). By
# default the OAuth library raises an exception then; we check granted scopes
# ourselves and show a clear message instead.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
SCOPES = IDENTITY_SCOPES + google_client.SCOPES
SYNC_SCOPES = set(google_client.SCOPES)


def _client_config(settings: Settings) -> dict:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",  # nosec B105: public endpoint URL, not a secret
            "redirect_uris": [settings.redirect_uri],
        }
    }


def _flow(settings: Settings, state: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        _client_config(settings), scopes=SCOPES, redirect_uri=settings.redirect_uri, state=state
    )
    return google_client.with_token_timeout(flow)


def authorization_url(settings: Settings, force_consent: bool) -> tuple[str, str, str]:
    """Return (url to send the browser to, state, PKCE code verifier)."""
    flow = _flow(settings)
    options = {"access_type": "offline", "include_granted_scopes": "false"}
    if force_consent:
        options["prompt"] = "consent"  # makes Google issue a refresh token again
    url, state = flow.authorization_url(**options)
    return url, state, flow.code_verifier


def exchange_code(settings: Settings, code: str, state: str, code_verifier: str) -> Credentials:
    flow = _flow(settings, state=state)
    flow.code_verifier = code_verifier
    # Passing the code directly (instead of the whole callback URL) also avoids
    # the library refusing http://localhost during local development.
    flow.fetch_token(code=code)
    return flow.credentials


def verify_identity(settings: Settings, creds: Credentials) -> dict:
    """Check the ID token's signature, issuer, audience and expiry; return its claims."""
    return id_token.verify_oauth2_token(creds.id_token, Request(), settings.google_client_id)


def has_sync_scopes(creds: Credentials) -> bool:
    granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    return SYNC_SCOPES <= granted
