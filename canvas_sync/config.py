"""Load configuration from .env. Secret values are never printed or included in error messages."""

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
PLACEHOLDER_MARKER = "REPLACE_ME"


class ConfigError(Exception):
    """Configuration is missing or invalid."""


def restrict_permissions(path: Path) -> None:
    """Make a secret file readable and writable only by its owner (chmod 600).

    Only applies on Linux/macOS. Windows doesn't use Unix permission bits; files
    under your user profile are already limited to you, SYSTEM and Administrators.
    """
    if os.name == "posix" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_private_file(path: Path, text: str) -> None:
    """Write a secret file atomically, readable only by its owner (on Linux/macOS).

    Writes to a temporary file first, then swaps it into place in one step, so a
    crash mid-write can never leave a half-written file behind.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    restrict_permissions(path)


ALLOWED_FEED_HOST_SUFFIX = ".instructure.com"  # Canvas cloud hosting
FEED_PATH_PREFIX = "/feeds/calendars/"


def validate_feed_url(raw: str) -> str:
    """Check that a URL is a Canvas calendar feed and return its normalized form.

    The checks are strict on purpose. A website that fetches URLs its users paste
    in can be abused to make the server request internal addresses (SSRF), so we
    only accept HTTPS links to Canvas feed paths. Error messages never repeat the URL.
    """
    raw = raw.strip()
    if not raw or PLACEHOLDER_MARKER in raw:
        raise ConfigError("The Canvas feed URL is not set.")

    parts = urlsplit(raw)
    # Canvas sometimes shows feeds as webcal:// links; that's the same URL over HTTPS.
    if parts.scheme == "webcal":
        parts = parts._replace(scheme="https")
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError("The feed URL must be an https:// (or webcal://) link.")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise ConfigError("The feed URL can't include a username, password or custom port.")
    if not parts.hostname.endswith(ALLOWED_FEED_HOST_SUFFIX):
        raise ConfigError("The feed URL must be on an instructure.com Canvas site.")
    if not parts.path.startswith(FEED_PATH_PREFIX) or not parts.path.endswith(".ics"):
        raise ConfigError("That doesn't look like a Canvas calendar feed link (…/feeds/calendars/….ics).")
    return urlunsplit(parts._replace(fragment=""))


def load_feed_url() -> str:
    restrict_permissions(ENV_FILE)
    load_dotenv(ENV_FILE)
    raw = os.environ.get("CANVAS_FEED_URL", "")
    if not raw.strip() or PLACEHOLDER_MARKER in raw:
        raise ConfigError(
            "CANVAS_FEED_URL is not set. Paste your Canvas calendar feed URL into .env."
        )
    return validate_feed_url(raw)
