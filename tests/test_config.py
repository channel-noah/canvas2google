import os

import pytest

from canvas_sync import config
from canvas_sync.config import ConfigError, load_feed_url, write_private_file


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point config at an empty temp .env so the real one is never read."""
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("CANVAS_FEED_URL", raising=False)


def test_missing_url_is_a_clear_error():
    with pytest.raises(ConfigError, match="not set"):
        load_feed_url()


def test_placeholder_url_is_rejected(monkeypatch):
    monkeypatch.setenv("CANVAS_FEED_URL", "https://x.instructure.com/feeds/calendars/user_REPLACE_ME.ics")
    with pytest.raises(ConfigError, match="not set"):
        load_feed_url()


def test_plain_http_is_rejected_without_echoing_url(monkeypatch):
    monkeypatch.setenv("CANVAS_FEED_URL", "http://x.instructure.com/feeds/user_SECRET.ics")
    with pytest.raises(ConfigError) as err:
        load_feed_url()
    assert "SECRET" not in str(err.value)


def test_webcal_is_upgraded_to_https(monkeypatch):
    monkeypatch.setenv("CANVAS_FEED_URL", "webcal://x.instructure.com/feeds/user_abc.ics")
    assert load_feed_url() == "https://x.instructure.com/feeds/user_abc.ics"


def test_private_file_write_is_atomic_and_owner_only(tmp_path):
    path = tmp_path / "secret.json"
    write_private_file(path, "one")
    write_private_file(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert not (tmp_path / "secret.json.tmp").exists()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
