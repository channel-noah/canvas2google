"""Canvas2Google web app (Flask).

Run locally with:  flask --app webapp run
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import click
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError, CSRFProtect

from canvas_sync.display import when_text
from canvas_sync.state import SyncState

from . import auth, views
from .crypto import SecretBox
from .db import get_db, init_db, load_state, save_state
from .settings import Settings, load_settings

# Content Security Policy: the browser may only load our own stylesheet. No
# scripts at all, no framing by other sites, forms only post back to us.
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


class RedactQueryStrings(logging.Filter):
    """Strip ?query strings from request log lines.

    Google's sign-in callback carries a one-time authorization code in its URL,
    and request logs would otherwise record it.
    """

    _query = re.compile(r"\?[^\s\"]*")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._query.sub("?[redacted]", record.getMessage())
        record.args = ()
        return True


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SETTINGS=settings,
        SECRET_BOX=SecretBox(settings.encryption_key),
        DATABASE_PATH=settings.database_path,
        # Session cookie: not readable by JavaScript, not sent on cross-site
        # requests, HTTPS-only once deployed, and expires after 12 hours.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        SESSION_COOKIE_NAME="__Host-session" if settings.secure_cookies else "session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=16 * 1024,  # forms here are tiny; reject big uploads
    )

    logging.getLogger("werkzeug").addFilter(RedactQueryStrings())
    CSRFProtect(app)  # every POST must carry a token only our pages know
    init_db(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.add_template_filter(when_text, "when")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Keeps OAuth codes and page URLs out of Referer headers sent to other sites.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"  # pages show personal data
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.cli.command("import-cli-state")
    @click.argument("email")
    def import_cli_state(email: str) -> None:
        """Copy the CLI's sync_state.json into EMAIL's web account.

        Lets the web app keep using the calendar, events and tasks the CLI
        already created, instead of creating a second "Canvas" calendar.
        """
        db = get_db()
        row = db.execute("SELECT id FROM users WHERE email = ?", (email.lower(),)).fetchone()
        if row is None:
            raise click.ClickException("No web user with that email. Sign in on the website first.")
        if load_state(db, row["id"]) is not None:
            raise click.ClickException("That account already has sync records; refusing to overwrite them.")
        state = SyncState.load()  # validates and migrates the file
        save_state(db, row["id"], state.to_dict())
        click.echo(f"Imported {len(state.events)} event(s) and {len(state.tasks)} task(s).")

    @app.errorhandler(CSRFError)
    def csrf_error(_exc):
        return render_template("error.html", message="Your form expired. Go back, refresh, and try again."), 400

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", message="Page not found."), 404

    @app.errorhandler(500)
    def server_error(_exc):
        # Never show a stack trace to the browser.
        return render_template("error.html", message="Something went wrong on our side."), 500

    return app
