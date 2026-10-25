"""Cloud deployment pieces: cron endpoint, HTTPS mode, privacy page, deletion."""

import dataclasses
from types import SimpleNamespace

import pytest

from fakes import FakeCalendarService, FakeTasksService
from helpers import SAMPLE_FEED, csrf_token, db_rows, save_feed_and_token
from webapp import create_app, oauth, sync_runner, views
from webapp.db import make_engine

SECRET = "c" * 40  # matches the test app's cron_secret


@pytest.fixture
def fake_google(monkeypatch):
    monkeypatch.setattr(views, "fetch_feed", lambda url: SAMPLE_FEED)  # dashboard preview
    monkeypatch.setattr(sync_runner, "fetch_feed", lambda url: SAMPLE_FEED)
    monkeypatch.setattr(sync_runner, "load_credentials", lambda db, box, uid: SimpleNamespace(token="t"))
    monkeypatch.setattr(sync_runner, "build_services", lambda creds: (FakeCalendarService(), FakeTasksService()))


# --- /tasks/sync-all ----------------------------------------------------------

@pytest.mark.parametrize("header", [None, "Bearer wrong", f"bearer {SECRET}", SECRET, f"Bearer {SECRET}x"])
def test_cron_endpoint_rejects_missing_or_wrong_secret(client, app, user_id, fake_google, header):
    save_feed_and_token(app, user_id)
    headers = {"Authorization": header} if header else {}
    assert client.get("/tasks/sync-all", headers=headers).status_code == 401
    assert db_rows(app, "SELECT * FROM sync_runs") == []


def test_cron_endpoint_runs_sync_with_correct_secret(client, app, user_id, fake_google):
    save_feed_and_token(app, user_id)
    response = client.get("/tasks/sync-all", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 200 and response.get_json() == {"users": 1, "failed": 0}
    assert db_rows(app, "SELECT trigger, ok FROM sync_runs") == [("cron", 1)]


def test_cron_endpoint_is_off_without_a_secret(app):
    no_secret = create_app(dataclasses.replace(app.config["SETTINGS"], cron_secret=None))
    assert no_secret.test_client().get("/tasks/sync-all", headers={"Authorization": "Bearer "}).status_code == 404


# --- HTTPS mode (as on Vercel) ------------------------------------------------

@pytest.fixture
def https_client(app):
    secure = create_app(dataclasses.replace(app.config["SETTINGS"], secure_cookies=True))
    secure.config["TESTING"] = True
    # Behave like Vercel's proxy: HTTPS ends there and is reported in a header.
    return secure.test_client(), {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "canvas.example"}


def test_https_mode_sets_hsts_and_hardened_cookie(https_client, monkeypatch):
    client, proxy = https_client
    monkeypatch.setattr(oauth, "authorization_url", lambda s, force_consent: ("https://accounts.google.com/x", "st", "v"))
    response = client.get("/auth/google", headers=proxy)
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("__Host-session=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


def test_https_form_posts_need_a_same_site_referer(https_client, user_id):
    client, proxy = https_client
    with client.session_transaction() as s:
        s["user_id"] = user_id
    page = client.get("/settings", headers=proxy).get_data(as_text=True)
    token = page.split('name="csrf_token" value="')[1].split('"')[0]
    form = {"timezone": "UTC", "csrf_token": token}

    # Without a Referer (what "no-referrer" would cause), Flask-WTF refuses the post.
    assert client.post("/settings/preferences", data=form, headers=proxy).status_code == 400
    # A Referer from another site is refused too.
    evil = {**proxy, "Referer": "https://evil.example/page"}
    assert client.post("/settings/preferences", data=form, headers=evil).status_code == 400
    # Same-site Referer (what "same-origin" sends) is accepted.
    good = {**proxy, "Referer": "https://canvas.example/settings"}
    assert client.post("/settings/preferences", data=form, headers=good).status_code == 302


# --- Public pages -------------------------------------------------------------

def test_privacy_page_is_public_and_covers_limited_use(client):
    page = client.get("/privacy")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "Limited Use" in text and "Delete my account" in text


def test_site_verification_tag_only_when_configured(app):
    assert "google-site-verification" not in app.test_client().get("/login").get_data(as_text=True)
    tagged = create_app(dataclasses.replace(app.config["SETTINGS"], site_verification="abc123"))
    assert '<meta name="google-site-verification" content="abc123">' in tagged.test_client().get("/login").get_data(as_text=True)


# --- Account deletion ---------------------------------------------------------

def test_delete_account_removes_everything_and_revokes(logged_in, app, user_id, fake_google, monkeypatch):
    save_feed_and_token(app, user_id)
    logged_in.post("/sync", data={"mode": "sync", "csrf_token": csrf_token(logged_in, "/")})
    revoked = []
    monkeypatch.setattr(sync_runner, "load_credentials",
                        lambda db, box, uid: SimpleNamespace(refresh_token="refresh-1"))
    monkeypatch.setattr(oauth, "revoke", lambda token: revoked.append(token) or True)

    response = logged_in.post("/settings/delete", data={"confirm": "on", "csrf_token": csrf_token(logged_in)})
    assert response.status_code == 302 and revoked == ["refresh-1"]
    for table in ("users", "user_secrets", "sync_state", "sync_runs"):
        assert db_rows(app, f"SELECT COUNT(*) FROM {table}") == [(0,)], table  # nosec B608 - fixed table names in a test
    assert logged_in.get("/").status_code == 302  # signed out


def test_delete_requires_confirmation(logged_in, app, user_id):
    logged_in.post("/settings/delete", data={"csrf_token": csrf_token(logged_in)})
    assert db_rows(app, "SELECT COUNT(*) FROM users") == [(1,)]


# --- Postgres URL handling ----------------------------------------------------

@pytest.mark.parametrize("url", ["postgres://u:p@db.example/app?sslmode=require",
                                 "postgresql://u:p@db.example/app?sslmode=require"])
def test_postgres_urls_use_psycopg(url):
    engine = make_engine(url)  # doesn't connect yet
    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.query["sslmode"] == "require"
