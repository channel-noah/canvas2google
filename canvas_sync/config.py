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


def load_feed_url() -> str:
    restrict_permissions(ENV_FILE)
    load_dotenv(ENV_FILE)
    raw = os.environ.get("CANVAS_FEED_URL", "").strip()
    if not raw or PLACEHOLDER_MARKER in raw:
        raise ConfigError(
            "CANVAS_FEED_URL is not set. Paste your Canvas calendar feed URL into .env."
        )

    parts = urlsplit(raw)
    # Canvas sometimes shows feeds as webcal:// links; that's the same URL over HTTPS.
    if parts.scheme == "webcal":
        parts = parts._replace(scheme="https")
    if parts.scheme != "https" or not parts.netloc:
        raise ConfigError("CANVAS_FEED_URL must be an https:// (or webcal://) link.")
    return urlunsplit(parts)
