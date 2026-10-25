"""Encryption key rotation: NEW,OLD keys, re-encryption, then retiring OLD."""

import dataclasses

import pytest
from cryptography.fernet import Fernet

from canvas_sync.config import ConfigError
from helpers import FEED, save_feed_and_token
from webapp import create_app, scheduler
from webapp.crypto import SecretBox, SecretError, secret_context
from webapp.db import get_db, get_secret, rotate_secrets, upsert_user

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


def test_new_data_uses_the_first_key_and_any_key_can_read():
    old_box = SecretBox(OLD)
    blob = old_box.encrypt("secret", "ctx")
    both = SecretBox(f"{NEW},{OLD}")
    assert both.decrypt(blob, "ctx") == "secret"        # old data still readable
    assert both.needs_rotation(blob)
    fresh = both.encrypt("secret", "ctx")
    assert not both.needs_rotation(fresh)                # new data uses NEW
    assert SecretBox(NEW).decrypt(fresh, "ctx") == "secret"


def test_rotate_keeps_plaintext_and_context_binding():
    both = SecretBox(f"{NEW},{OLD}")
    rotated = both.rotate(SecretBox(OLD).encrypt("secret", secret_context(1, "feed_url")))
    new_only = SecretBox(NEW)
    assert new_only.decrypt(rotated, secret_context(1, "feed_url")) == "secret"
    with pytest.raises(SecretError):  # still bound to its original row
        new_only.decrypt(rotated, secret_context(2, "feed_url"))


def test_unknown_key_cannot_be_rotated():
    stranger = SecretBox(Fernet.generate_key().decode()).encrypt("x", "ctx")
    with pytest.raises(SecretError):
        SecretBox(f"{NEW},{OLD}").rotate(stranger)


@pytest.mark.parametrize("value", ["", "not-a-key", f"{NEW},not-a-key"])
def test_invalid_key_lists_are_rejected(value):
    with pytest.raises(ConfigError):
        SecretBox(value)


def _app_with_keys(app, keys):
    rotated = create_app(dataclasses.replace(app.config["SETTINGS"], encryption_key=keys))
    rotated.config["TESTING"] = True
    return rotated


def test_full_rotation_procedure(app, user_id, monkeypatch):
    """The documented procedure: deploy NEW,OLD → daily sync re-encrypts → drop OLD."""
    from types import SimpleNamespace

    from webapp import sync_runner

    old_app = _app_with_keys(app, OLD)
    save_feed_and_token(old_app, user_id)  # data written under OLD

    # Only the rotation step matters here; skip the actual Canvas/Google sync.
    monkeypatch.setattr(sync_runner, "run_sync", lambda *a, **k: SimpleNamespace(ok=True, error=None))
    both = _app_with_keys(app, f"{NEW},{OLD}")
    scheduler.sync_all_users(both)  # the daily sync finishes the rotation
    with both.app_context():
        db = get_db()
        blob = get_secret(db, user_id, "feed_url")
        assert not both.config["SECRET_BOX"].needs_rotation(blob)

    new_only = _app_with_keys(app, NEW)  # OLD retired
    with new_only.app_context():
        box = new_only.config["SECRET_BOX"]
        db = get_db()
        assert box.decrypt(get_secret(db, user_id, "feed_url"), secret_context(user_id, "feed_url")) == FEED
        assert box.decrypt(get_secret(db, user_id, "google_token"), secret_context(user_id, "google_token")) == "{}"


def test_rotation_reports_unreadable_values_without_touching_them(app, user_id):
    stranger_box = SecretBox(Fernet.generate_key().decode())
    with app.app_context():
        db = get_db()
        other = upsert_user(db, "sub-x", "x@example.com")
        from webapp.db import set_secret
        foreign = stranger_box.encrypt("x", secret_context(other, "feed_url"))
        set_secret(db, other, "feed_url", foreign)
        rotated, unreadable = rotate_secrets(db, app.config["SECRET_BOX"])
        assert (rotated, unreadable) == (0, 1)
        assert get_secret(db, other, "feed_url") == foreign


def test_rotate_keys_command(app, user_id):
    old_app = _app_with_keys(app, OLD)
    save_feed_and_token(old_app, user_id)
    result = _app_with_keys(app, f"{NEW},{OLD}").test_cli_runner().invoke(args=["rotate-keys"])
    assert result.exit_code == 0 and "Re-encrypted 2 secret(s)" in result.output
