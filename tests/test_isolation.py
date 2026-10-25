"""Multi-user isolation: one user can never see, change or delete another's data.

Two users (Alice and Bob) each get their own feed, Google token and Google
services. Every test checks that acting as one leaves the other untouched.
"""

from types import SimpleNamespace

import pytest
from flask.sessions import SecureCookieSessionInterface

from fakes import FakeCalendarService, FakeTasksService
from helpers import csrf_token, db_rows
from webapp import scheduler, sync_runner, views
from webapp.crypto import SecretError, secret_context
from webapp.db import get_db, get_secret, load_state, set_secret, upsert_user

ALICE_FEED = "https://alice-school.instructure.com/feeds/calendars/user_ALICE.ics"
BOB_FEED = "https://bob-school.instructure.com/feeds/calendars/user_BOB.ics"


def feed_for(owner: str, title: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
        f"BEGIN:VEVENT\r\nUID:event-assignment-{abs(hash(owner)) % 10000}\r\n"
        "DTSTART;VALUE=DATE:20991101\r\nDTEND;VALUE=DATE:20991102\r\n"
        f"SUMMARY:{title} [CS-101-01]\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


FEEDS = {ALICE_FEED: feed_for("alice", "Alice homework"), BOB_FEED: feed_for("bob", "Bob homework")}


@pytest.fixture
def two_users(app, monkeypatch):
    """Alice (the logged-in fixture user's allowlist entry) and Bob, each fully set up."""
    box = app.config["SECRET_BOX"]
    with app.app_context():
        db = get_db()
        alice = upsert_user(db, "sub-alice", "noah@example.com")  # matches the test allowlist
        bob = upsert_user(db, "sub-bob", "bob@example.com")
        for uid, feed in ((alice, ALICE_FEED), (bob, BOB_FEED)):
            set_secret(db, uid, "feed_url", box.encrypt(feed, secret_context(uid, "feed_url")))
            set_secret(db, uid, "google_token", box.encrypt(f'{{"owner": {uid}}}', secret_context(uid, "google_token")))

    # Each user's token leads to their own Google account (separate fake services).
    services = {alice: (FakeCalendarService(), FakeTasksService()), bob: (FakeCalendarService(), FakeTasksService())}
    feeds_fetched = []

    def fetch(url):
        feeds_fetched.append(url)
        return FEEDS[url]

    monkeypatch.setattr(sync_runner, "fetch_feed", fetch)
    monkeypatch.setattr(views, "fetch_feed", fetch)
    monkeypatch.setattr(sync_runner, "load_credentials",
                        lambda db, box, uid: SimpleNamespace(token=f"token-{uid}", refresh_token=None, owner=uid))
    monkeypatch.setattr(sync_runner, "build_services", lambda creds: services[creds.owner])
    return SimpleNamespace(alice=alice, bob=bob, services=services, feeds_fetched=feeds_fetched)


def sign_in_as(client, user_id):
    with client.session_transaction() as s:
        s.clear()
        s["user_id"] = user_id


def tasks_in(services):
    return [t["title"] for t in services[1].tasks_by_id.values()]


# --- Reading ------------------------------------------------------------------

def test_dashboard_shows_only_your_own_data(client, two_users):
    sign_in_as(client, two_users.alice)
    page = client.get("/").get_data(as_text=True)
    assert "alice-school.instructure.com" in page and "Alice homework" in page
    assert "bob" not in page.lower()


def test_settings_never_reveal_another_users_feed(client, two_users):
    sign_in_as(client, two_users.bob)
    page = client.get("/settings").get_data(as_text=True)
    assert "bob-school.instructure.com" in page
    assert "alice" not in page.lower()


def test_sync_history_is_per_user(client, app, two_users):
    sign_in_as(client, two_users.alice)
    client.post("/sync", data={"mode": "sync", "csrf_token": csrf_token(client, "/")})
    sign_in_as(client, two_users.bob)
    page = client.get("/").get_data(as_text=True)
    assert "No syncs yet." in page  # Alice's run doesn't appear on Bob's dashboard


# --- Syncing ------------------------------------------------------------------

def test_sync_uses_only_your_feed_token_and_google_account(client, app, two_users):
    sign_in_as(client, two_users.alice)
    client.post("/sync", data={"mode": "sync", "csrf_token": csrf_token(client, "/")})

    assert tasks_in(two_users.services[two_users.alice]) == ["Alice homework (CS-101-01)"]
    assert tasks_in(two_users.services[two_users.bob]) == []  # Bob's Google account untouched
    assert BOB_FEED not in two_users.feeds_fetched
    with app.app_context():
        assert load_state(get_db(), two_users.bob) is None  # Bob's sync records untouched


def test_scheduled_sync_keeps_every_user_separate(app, two_users):
    scheduler.sync_all_users(app)
    assert tasks_in(two_users.services[two_users.alice]) == ["Alice homework (CS-101-01)"]
    assert tasks_in(two_users.services[two_users.bob]) == ["Bob homework (CS-101-01)"]
    with app.app_context():
        db = get_db()
        alice_state, bob_state = load_state(db, two_users.alice), load_state(db, two_users.bob)
    assert set(alice_state["tasks"]).isdisjoint(bob_state["tasks"])
    assert alice_state["tasklist_id"] in two_users.services[two_users.alice][1].tasklists_by_id
    assert bob_state["tasklist_id"] in two_users.services[two_users.bob][1].tasklists_by_id


def test_one_users_sync_lock_doesnt_block_another(app, two_users):
    from webapp.db import try_lock

    with app.app_context():
        db = get_db()
        assert try_lock(db, f"sync:user:{two_users.alice}")  # Alice mid-sync
        outcome = sync_runner.run_sync(db, app.config["SECRET_BOX"], two_users.bob, dry_run=False)
    assert outcome.ok


# --- Writing and deleting -----------------------------------------------------

def test_saving_settings_changes_only_your_row(client, app, two_users):
    new_feed = "https://alice-new.instructure.com/feeds/calendars/user_NEW.ics"
    sign_in_as(client, two_users.alice)
    client.post("/settings", data={"feed_url": new_feed, "csrf_token": csrf_token(client)})
    client.post("/settings/preferences", data={"timezone": "UTC", "csrf_token": csrf_token(client)})

    box = app.config["SECRET_BOX"]
    with app.app_context():
        db = get_db()
        assert box.decrypt(get_secret(db, two_users.bob, "feed_url"), secret_context(two_users.bob, "feed_url")) == BOB_FEED
    assert db_rows(app, "SELECT timezone FROM users WHERE id = ?", two_users.bob) == [(None,)]


def test_deleting_your_account_leaves_others_intact(client, app, two_users, monkeypatch):
    from webapp import oauth

    monkeypatch.setattr(oauth, "revoke", lambda token: True)
    scheduler.sync_all_users(app)  # both have state and history
    sign_in_as(client, two_users.alice)
    client.post("/settings/delete", data={"confirm": "on", "csrf_token": csrf_token(client)})

    for table in ("user_secrets", "sync_state", "sync_runs"):
        rows = db_rows(app, f"SELECT user_id FROM {table}")  # nosec B608 - fixed table names in a test
        assert rows and all(uid == two_users.bob for (uid,) in rows), table
    assert db_rows(app, "SELECT email FROM users") == [("bob@example.com",)]


def test_history_is_pruned_per_user_without_touching_others(app, two_users):
    from webapp.db import KEEP_RUNS, finish_run, start_run

    with app.app_context():
        db = get_db()
        for _ in range(3):
            finish_run(db, start_run(db, two_users.bob, dry_run=False), True, None, None)
        for _ in range(KEEP_RUNS + 5):
            finish_run(db, start_run(db, two_users.alice, dry_run=False), True, None, None)

    alice_ids = [i for (i,) in db_rows(app, "SELECT id FROM sync_runs WHERE user_id = ? ORDER BY id", two_users.alice)]
    assert len(alice_ids) == KEEP_RUNS
    assert alice_ids == sorted(alice_ids)[-KEEP_RUNS:]  # the newest ones were kept
    assert db_rows(app, "SELECT COUNT(*) FROM sync_runs WHERE user_id = ?", two_users.bob) == [(3,)]


# --- Tampering ----------------------------------------------------------------

def test_no_route_takes_a_user_id(app):
    # Every page acts on the signed-in user from the session. A URL like
    # /users/7/settings would invite "change the number" attacks (IDOR).
    for rule in app.url_map.iter_rules():
        assert not ({"user_id", "uid", "id"} & set(rule.arguments)), rule.rule


def test_forged_session_cookie_is_rejected(client, two_users):
    # A cookie signed with a different secret key must not log anyone in.
    serializer = SecureCookieSessionInterface().get_signing_serializer(
        SimpleNamespace(secret_key="attacker-guess", config={"SECRET_KEY_FALLBACKS": []})
    )
    client.set_cookie("session", serializer.dumps({"user_id": two_users.bob}))
    response = client.get("/")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")


def test_ciphertext_copied_into_another_row_is_refused(app, client, two_users):
    with app.app_context():
        db = get_db()
        set_secret(db, two_users.bob, "feed_url", get_secret(db, two_users.alice, "feed_url"))
        with pytest.raises(SecretError):
            sync_runner.load_feed_url(db, app.config["SECRET_BOX"], two_users.bob)
    sign_in_as(client, two_users.bob)
    page = client.get("/settings").get_data(as_text=True)
    assert "alice" not in page.lower() and "doesn" in page  # an error, never Alice's feed
