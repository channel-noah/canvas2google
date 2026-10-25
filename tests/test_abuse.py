"""Abuse controls: rate limits, the user cap, and invite-only vs open sign-up."""

import dataclasses
from types import SimpleNamespace

import pytest

from canvas_sync.config import ConfigError
from fakes import FakeCalendarService, FakeTasksService
from helpers import SAMPLE_FEED, csrf_token, db_rows, fake_google, save_feed_and_token, start_sign_in
from webapp import create_app, db as store, limits, sync_runner, views
from webapp.db import get_db, upsert_user

SECRET = "c" * 40


def callback(client, **environ):
    return client.get("/auth/callback?state=state-123&code=abc", environ_base=environ)


# --- The limiter itself -------------------------------------------------------

def test_counter_counts_within_a_window_and_resets_in_the_next(app, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    with app.app_context():
        db = get_db()
        assert [store.count_hit(db, "k", 600) for _ in range(3)] == [1, 2, 3]
        assert store.count_hit(db, "other", 600) == 1  # keys are independent
        now[0] += 600  # next window
        assert store.count_hit(db, "k", 600) == 1


def test_raw_ip_addresses_are_never_stored(client, app, monkeypatch):
    start_sign_in(client, monkeypatch, REMOTE_ADDR="203.0.113.7")
    keys = [k for (k,) in db_rows(app, "SELECT key FROM rate_limits")]
    assert keys and not any("203.0.113.7" in k for k in keys)
    with app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.7"}):
        same = limits.ip_label()
    with app.test_request_context(environ_base={"REMOTE_ADDR": "198.51.100.9"}):
        other = limits.ip_label()
    assert keys[0].endswith(same) and same != other


# --- Sign-in ------------------------------------------------------------------

def test_sign_in_attempts_are_limited_per_ip(client, monkeypatch):
    for _ in range(limits.SIGN_IN.max_hits):
        start_sign_in(client, monkeypatch, REMOTE_ADDR="203.0.113.7")
    blocked = client.get("/auth/google", environ_base={"REMOTE_ADDR": "203.0.113.7"})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == str(limits.SIGN_IN.window_seconds)
    start_sign_in(client, monkeypatch, REMOTE_ADDR="198.51.100.9")  # a different visitor isn't affected


# --- Sync and dashboard -------------------------------------------------------

@pytest.fixture
def fake_sync(monkeypatch):
    fetches = []
    monkeypatch.setattr(views, "fetch_feed", lambda url: fetches.append(url) or SAMPLE_FEED)
    monkeypatch.setattr(sync_runner, "fetch_feed", lambda url: SAMPLE_FEED)
    monkeypatch.setattr(sync_runner, "load_credentials", lambda db, box, uid: SimpleNamespace(token="t"))
    monkeypatch.setattr(sync_runner, "build_services", lambda creds: (FakeCalendarService(), FakeTasksService()))
    return fetches


def test_manual_syncs_are_limited_per_user(logged_in, app, user_id, fake_sync):
    save_feed_and_token(app, user_id)
    token = csrf_token(logged_in, "/")
    codes = [logged_in.post("/sync", data={"mode": "preview", "csrf_token": token}).status_code
             for _ in range(limits.SYNC.max_hits + 1)]
    assert codes[:-1] == [200] * limits.SYNC.max_hits and codes[-1] == 429

    # Another user's allowance is separate.
    with app.app_context():
        other = upsert_user(get_db(), "sub-other", "other@example.com")
    save_feed_and_token(app, other)
    with logged_in.session_transaction() as s:
        s["user_id"] = other
    assert logged_in.post("/sync", data={"mode": "preview", "csrf_token": csrf_token(logged_in, "/")}).status_code == 200


def test_dashboard_stops_fetching_canvas_when_refreshed_too_often(logged_in, app, user_id, fake_sync):
    save_feed_and_token(app, user_id)
    for _ in range(limits.FEED_FETCH.max_hits):
        logged_in.get("/")
    fetched_before = len(fake_sync)
    page = logged_in.get("/").get_data(as_text=True)
    assert len(fake_sync) == fetched_before  # no Canvas request this time
    assert "refreshed a lot recently" in page


# --- Cron endpoint ------------------------------------------------------------

def test_wrong_cron_secrets_lock_out_that_ip(client, app, user_id, fake_sync):
    save_feed_and_token(app, user_id)
    attacker = {"REMOTE_ADDR": "203.0.113.66"}
    for _ in range(limits.CRON_FAILURES.max_hits):
        assert client.get("/tasks/sync-all", headers={"Authorization": "Bearer guess"}, environ_base=attacker).status_code == 401
    # Now even the right secret from that IP isn't checked, so guessing can't continue.
    right = {"Authorization": f"Bearer {SECRET}"}
    assert client.get("/tasks/sync-all", headers=right, environ_base=attacker).status_code == 429
    # The real cron (another IP) is unaffected, and successes are never counted as failures.
    for _ in range(limits.CRON_FAILURES.max_hits + 1):
        assert client.get("/tasks/sync-all", headers=right, environ_base={"REMOTE_ADDR": "198.51.100.1"}).status_code == 200


# --- Sign-up mode and user cap ------------------------------------------------

def open_app(app, **changes):
    settings = dataclasses.replace(app.config["SETTINGS"], signup_mode="open", **changes)
    opened = create_app(settings)
    opened.config["TESTING"] = True
    return opened


def test_invite_mode_refuses_strangers(client, monkeypatch, app):
    start_sign_in(client, monkeypatch)
    revoked = fake_google(monkeypatch, "stranger@example.org")
    assert callback(client).status_code == 403 and revoked
    assert db_rows(app, "SELECT * FROM users") == []


def test_open_mode_lets_any_verified_account_sign_up(app, monkeypatch):
    opened = open_app(app)
    client = opened.test_client()
    start_sign_in(client, monkeypatch)
    revoked = fake_google(monkeypatch, "stranger@example.org")
    assert callback(client).status_code == 302 and revoked == []
    assert db_rows(opened, "SELECT email FROM users") == [("stranger@example.org",)]


def test_open_mode_still_requires_a_verified_email(app, monkeypatch):
    client = open_app(app).test_client()
    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, "stranger@example.org", verified=False)
    assert callback(client).status_code == 403


def test_user_cap_blocks_new_accounts_but_not_returning_ones(app, monkeypatch):
    opened = open_app(app, max_users=1)
    client = opened.test_client()
    with opened.app_context():
        upsert_user(get_db(), "sub-existing", "existing@example.org")

    start_sign_in(client, monkeypatch)
    revoked = fake_google(monkeypatch, "newcomer@example.org")
    response = callback(client)
    assert response.status_code == 403 and "accepting new accounts" in response.get_data(as_text=True)
    assert revoked  # their fresh grant is handed back

    start_sign_in(client, monkeypatch)
    fake_google(monkeypatch, "existing@example.org", sub="sub-existing")
    assert callback(client).status_code == 302  # the existing user can still sign in


def test_login_page_wording_matches_mode(app):
    assert "limited to invited accounts" in app.test_client().get("/login").get_data(as_text=True)
    assert "limited to invited accounts" not in open_app(app).test_client().get("/login").get_data(as_text=True)


# --- Settings validation --------------------------------------------------------

@pytest.fixture
def base_env(monkeypatch):
    for name in ("FLASK_SECRET_KEY", "APP_ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "x")
    for name in ("ALLOWED_EMAILS", "SIGNUP_MODE", "MAX_USERS"):
        monkeypatch.delenv(name, raising=False)
    from canvas_sync import config
    monkeypatch.setattr(config, "ENV_FILE", config.PROJECT_ROOT / "no-such.env")
    import webapp.settings as settings_module
    monkeypatch.setattr(settings_module, "ENV_FILE", config.PROJECT_ROOT / "no-such.env")
    return monkeypatch


def test_invite_mode_requires_an_allowlist(base_env):
    from webapp.settings import load_settings
    with pytest.raises(ConfigError, match="ALLOWED_EMAILS"):
        load_settings()


def test_open_mode_needs_no_allowlist(base_env):
    from webapp.settings import load_settings
    base_env.setenv("SIGNUP_MODE", "open")
    settings = load_settings()
    assert settings.signup_mode == "open" and settings.max_users == 90


@pytest.mark.parametrize("name, value", [("SIGNUP_MODE", "everyone"), ("MAX_USERS", "0"), ("MAX_USERS", "lots")])
def test_bad_signup_settings_are_rejected(base_env, name, value):
    from webapp.settings import load_settings
    base_env.setenv("ALLOWED_EMAILS", "a@b.c")
    base_env.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()
