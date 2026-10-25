"""Web app tests: sign-in rules, CSRF, encryption at rest, escaping and syncing.

Google and Canvas are replaced with fakes; nothing leaves the machine.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from fakes import FakeCalendarService, FakeTasksService
from helpers import ALLOWED, FEED, SAMPLE_FEED, csrf_token, db_rows, make_item
from webapp import oauth, sync_runner, views
from webapp.crypto import SecretBox, SecretError, secret_context
from webapp.db import get_db


# --- Access control and headers -------------------------------------------

def test_pages_require_sign_in(client):
    for path in ("/", "/settings"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert client.post("/sync").status_code in (302, 400)  # never runs a sync


def test_security_headers_are_set(client):
    headers = client.get("/login").headers
    assert "script-src" not in headers["Content-Security-Policy"]  # no scripts allowed at all
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_request_logs_redact_query_strings():
    import logging
    from webapp import RedactQueryStrings

    record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                               '127.0.0.1 - - "%s" 302 -', ("GET /auth/callback?state=s&code=4/SECRETCODE HTTP/1.1",), None)
    RedactQueryStrings().filter(record)
    assert "SECRETCODE" not in record.getMessage()
    assert "/auth/callback?[redacted]" in record.getMessage()


def test_errors_never_show_stack_traces(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404 and "Traceback" not in response.get_data(as_text=True)


# --- Sign-in ----------------------------------------------------------------

def start_sign_in(client, monkeypatch):
    monkeypatch.setattr(oauth, "authorization_url",
                        lambda settings, force_consent: ("https://accounts.google.com/o/oauth2/auth?x", "state-123", "verifier"))
    response = client.get("/auth/google")
    assert response.status_code == 302 and response.headers["Location"].startswith("https://accounts.google.com/")
    return response


def fake_google(monkeypatch, email, verified=True, scopes=None, refresh_token="refresh-SECRET"):
    creds = SimpleNamespace(
        refresh_token=refresh_token,
        granted_scopes=scopes if scopes is not None else oauth.SCOPES,
        scopes=None,
        to_json=lambda: '{"refresh_token": "refresh-SECRET"}',
    )
    monkeypatch.setattr(oauth, "exchange_code", lambda *a: creds)
    monkeypatch.setattr(oauth, "verify_identity",
                        lambda settings, c: {"email": email, "email_verified": verified, "sub": "sub-" + email})


def test_session_cookie_is_httponly_and_samesite(client, monkeypatch):
    cookie = start_sign_in(client, monkeypatch).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_callback_with_wrong_state_is_rejected(client, monkeypatch, app):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, ALLOWED)
    response = client.get("/auth/callback?state=attacker-state&code=abc")
    assert response.status_code == 400
    assert db_rows(app, "SELECT * FROM users") == []


def test_callback_without_starting_sign_in_is_rejected(client, monkeypatch):
    fake_google(monkeypatch, ALLOWED)
    assert client.get("/auth/callback?state=state-123&code=abc").status_code == 400


def test_account_not_on_allowlist_is_refused_and_nothing_stored(client, monkeypatch, app):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, "intruder@evil.com")
    response = client.get("/auth/callback?state=state-123&code=abc")
    assert response.status_code == 403
    assert db_rows(app, "SELECT * FROM users") == []
    assert client.get("/").status_code == 302  # still signed out


def test_unverified_email_is_refused(client, monkeypatch):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, ALLOWED, verified=False)
    assert client.get("/auth/callback?state=state-123&code=abc").status_code == 403


def test_missing_sync_permissions_are_refused(client, monkeypatch):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, ALLOWED, scopes=oauth.IDENTITY_SCOPES)
    assert client.get("/auth/callback?state=state-123&code=abc").status_code == 400


def test_allowed_sign_in_stores_token_encrypted(client, monkeypatch, app):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, ALLOWED)
    response = client.get("/auth/callback?state=state-123&code=abc")
    assert response.status_code == 302 and response.headers["Location"].endswith("/")

    (blob,) = db_rows(app, "SELECT google_token FROM user_secrets")[0]
    assert b"refresh-SECRET" not in blob
    (uid,) = db_rows(app, "SELECT id FROM users")[0]
    assert "refresh-SECRET" in app.config["SECRET_BOX"].decrypt(blob, secret_context(uid, "google_token"))


def test_state_can_only_be_used_once(client, monkeypatch):
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, ALLOWED)
    client.get("/auth/callback?state=state-123&code=abc")
    client.post("/logout", data={"csrf_token": csrf_token(client, "/")})
    assert client.get("/auth/callback?state=state-123&code=abc").status_code == 400


# --- CSRF and settings ------------------------------------------------------

def test_posts_without_csrf_token_are_rejected(logged_in, app):
    assert logged_in.post("/settings", data={"feed_url": FEED}).status_code == 400
    assert logged_in.post("/sync", data={"mode": "sync"}).status_code == 400
    assert db_rows(app, "SELECT * FROM user_secrets") == []


def test_feed_url_is_stored_encrypted_and_never_shown(logged_in, app, user_id):
    logged_in.post("/settings", data={"feed_url": FEED, "csrf_token": csrf_token(logged_in)})

    raw_db = app.config["DATABASE_PATH"].read_bytes()
    assert b"SECRETFEED123" not in raw_db  # not in the database file, even in raw bytes
    page = logged_in.get("/settings").get_data(as_text=True)
    assert "school.instructure.com" in page and "SECRETFEED123" not in page


@pytest.mark.parametrize("bad", ["http://school.instructure.com/feeds/calendars/a.ics",
                                 "https://169.254.169.254/latest/meta-data/",
                                 "https://evil.com/feeds/calendars/a.ics"])
def test_unsafe_feed_urls_are_rejected(logged_in, app, bad):
    response = logged_in.post("/settings", data={"feed_url": bad, "csrf_token": csrf_token(logged_in)},
                              follow_redirects=True)
    assert "flash error" in response.get_data(as_text=True)
    assert db_rows(app, "SELECT * FROM user_secrets") == []


def test_encrypted_value_cannot_be_moved_to_another_user():
    box = SecretBox(Fernet.generate_key().decode())
    blob = box.encrypt("secret", secret_context(1, "feed_url"))
    assert box.decrypt(blob, secret_context(1, "feed_url")) == "secret"
    with pytest.raises(SecretError):
        box.decrypt(blob, secret_context(2, "feed_url"))


# --- Dashboard escaping -----------------------------------------------------

def test_feed_content_is_html_escaped(logged_in, app, user_id, monkeypatch):
    with app.app_context():
        from webapp.db import set_secret
        set_secret(get_db(), user_id, "feed_url",
                   app.config["SECRET_BOX"].encrypt(FEED, secret_context(user_id, "feed_url")))
    evil = make_item("event-assignment-1", "<script>alert(1)</script>", course="<b>CS</b>")
    monkeypatch.setattr(views, "fetch_feed", lambda url: b"")
    monkeypatch.setattr(views, "parse_feed", lambda data: [evil])

    html = logged_in.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<b>CS</b>" not in html


# --- Syncing through the web app -------------------------------------------

def test_sync_now_then_preview_shows_no_changes(logged_in, app, user_id, monkeypatch):
    with app.app_context():
        from webapp.db import set_secret
        set_secret(get_db(), user_id, "feed_url",
                   app.config["SECRET_BOX"].encrypt(FEED, secret_context(user_id, "feed_url")))
    calendar, tasks = FakeCalendarService(), FakeTasksService()
    monkeypatch.setattr(sync_runner, "fetch_feed", lambda url: SAMPLE_FEED)
    monkeypatch.setattr(sync_runner, "load_credentials", lambda db, box, uid: SimpleNamespace(token="t"))
    monkeypatch.setattr(sync_runner, "build_services", lambda creds: (calendar, tasks))
    monkeypatch.setattr(views, "fetch_feed", lambda url: SAMPLE_FEED)

    token = csrf_token(logged_in, "/")
    page = logged_in.post("/sync", data={"mode": "sync", "csrf_token": token}).get_data(as_text=True)
    assert "Sync complete" in page and "created: Homework 3" in page
    assert len(tasks.tasks_by_id) == 3 and len(calendar.events_by_id) == 1

    page = logged_in.post("/sync", data={"mode": "preview", "csrf_token": token}).get_data(as_text=True)
    assert "nothing was changed" in page and "0 new · 0 changed" in page

    runs = db_rows(app, "SELECT dry_run, ok FROM sync_runs ORDER BY id")
    assert runs == [(0, 1), (1, 1)]
    assert "Recent syncs" in logged_in.get("/").get_data(as_text=True)


def test_sync_without_feed_explains_what_to_do(logged_in, monkeypatch):
    page = logged_in.post("/sync", data={"mode": "sync", "csrf_token": csrf_token(logged_in, "/")})
    assert "Add your Canvas feed URL" in page.get_data(as_text=True)
