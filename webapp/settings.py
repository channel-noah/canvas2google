"""Web app configuration, read from environment variables.

Locally these come from .env; on a cloud host they'll be the host's secret
settings. Error messages name missing settings but never show their values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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
    # Local SQLite file by default; a Postgres URL in the cloud.
    database_url: str = "sqlite:///" + (PROJECT_ROOT / "instance" / "canvas2google.db").as_posix()
    secure_cookies: bool = False  # true once served over HTTPS
    sync_interval_hours: float = 3.0
    cron_secret: str | None = None  # enables /tasks/sync-all when set
    site_verification: str | None = None  # Google Search Console meta-tag value
    on_vercel: bool = False  # automatic sync is Vercel's daily cron, not the in-app scheduler

    @property
    def auto_sync_schedule(self) -> str:
        """How often automatic sync runs, in words, for the Settings and Dashboard pages."""
        if self.on_vercel:
            return "once a day (early morning, Eastern)"
        return f"every {self.sync_interval_hours:g} hours"


def load_settings() -> Settings:
    restrict_permissions(ENV_FILE)
    load_dotenv(ENV_FILE)
    env = {name: os.environ.get(name, "").strip() for name in REQUIRED}
    missing = [name for name, value in env.items() if not value or PLACEHOLDER_MARKER in value]
    if missing:
        raise ConfigError("Missing web app settings in .env: " + ", ".join(missing))

    allowed = frozenset(e.strip().lower() for e in env["ALLOWED_EMAILS"].split(",") if e.strip())
    try:
        interval = float(os.environ.get("SYNC_INTERVAL_HOURS", Settings.sync_interval_hours))
    except ValueError:
        raise ConfigError("SYNC_INTERVAL_HOURS must be a number.") from None
    if interval < 0.25:
        # Polling Canvas and Google more often than every 15 minutes gains little
        # and risks hitting API rate limits.
        raise ConfigError("SYNC_INTERVAL_HOURS must be at least 0.25 (15 minutes).")
    database_url = os.environ.get("DATABASE_URL", "").strip() or Settings.database_url
    if os.environ.get("VERCEL") and database_url.startswith("sqlite"):
        # Vercel's disk is temporary and not shared between instances: a SQLite
        # file there would seem to work, then lose everyone's data. Refuse to start.
        raise ConfigError("DATABASE_URL must point to Postgres on Vercel (connect the Neon database).")
    cron_secret = os.environ.get("CRON_SECRET", "").strip() or None
    if cron_secret and len(cron_secret) < 32:
        # This token is the only thing protecting the sync endpoint; make it unguessable.
        raise ConfigError("CRON_SECRET must be at least 32 characters.")
    return Settings(
        secret_key=env["FLASK_SECRET_KEY"],
        encryption_key=env["APP_ENCRYPTION_KEY"],
        google_client_id=env["GOOGLE_CLIENT_ID"],
        google_client_secret=env["GOOGLE_CLIENT_SECRET"],
        allowed_emails=allowed,
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", Settings.redirect_uri),
        database_url=database_url,
        secure_cookies=os.environ.get("SECURE_COOKIES", "false").lower() == "true",
        sync_interval_hours=interval,
        cron_secret=cron_secret,
        site_verification=os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip() or None,
        on_vercel=bool(os.environ.get("VERCEL")),
    )
