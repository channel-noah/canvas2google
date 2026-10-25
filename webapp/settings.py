"""Web app configuration, read from environment variables.

Locally these come from .env; on a cloud host they'll be the host's secret
settings. Error messages name missing settings but never show their values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from canvas_sync.config import ENV_FILE, PLACEHOLDER_MARKER, PROJECT_ROOT, ConfigError, restrict_permissions

REQUIRED = (
    "FLASK_SECRET_KEY",
    "APP_ENCRYPTION_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "ALLOWED_EMAILS",
)


@dataclass(frozen=True)
class Settings:
    secret_key: str
    encryption_key: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    redirect_uri: str = "http://localhost:5000/auth/callback"
    database_path: Path = PROJECT_ROOT / "instance" / "canvas2google.db"
    secure_cookies: bool = False  # true once served over HTTPS


def load_settings() -> Settings:
    restrict_permissions(ENV_FILE)
    load_dotenv(ENV_FILE)
    env = {name: os.environ.get(name, "").strip() for name in REQUIRED}
    missing = [name for name, value in env.items() if not value or PLACEHOLDER_MARKER in value]
    if missing:
        raise ConfigError("Missing web app settings in .env: " + ", ".join(missing))

    allowed = frozenset(e.strip().lower() for e in env["ALLOWED_EMAILS"].split(",") if e.strip())
    return Settings(
        secret_key=env["FLASK_SECRET_KEY"],
        encryption_key=env["APP_ENCRYPTION_KEY"],
        google_client_id=env["GOOGLE_CLIENT_ID"],
        google_client_secret=env["GOOGLE_CLIENT_SECRET"],
        allowed_emails=allowed,
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", Settings.redirect_uri),
        database_path=Path(os.environ.get("DATABASE_PATH", str(Settings.database_path))),
        secure_cookies=os.environ.get("SECURE_COOKIES", "false").lower() == "true",
    )
