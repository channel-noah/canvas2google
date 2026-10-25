"""Canvas2Google web app (Flask).

Run locally with:            flask --app webapp run
...with automatic syncing:   flask --app "webapp:create_app(start_scheduler=True)" run
One-off sync for cron:       flask --app webapp sync-all
On Vercel:                   app.py exposes `app`; a daily cron calls /tasks/sync-all
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import click
from flask import Flask, render_template
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from canvas_sync.display import when_text
from canvas_sync.state import SyncState

from . import auth, public, scheduler, tasks, views
from .crypto import SecretBox
from .db import find_user_id, get_db, init_db, load_state, save_state
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


def create_app(settings: Settings | None = None, start_scheduler: bool = False) -> Flask:
    """Build the app. start_scheduler=True also starts automatic syncing; it's
    off by default so tests and one-off commands never start background work."""
    settings = settings or load_settings()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SETTINGS=settings,
        SECRET_BOX=SecretBox(settings.encryption_key),
        DATABASE_URL=settings.database_url,
        # Session cookie: not readable by JavaScript, not sent on cross-site
        # requests, HTTPS-only once deployed, and expires after 12 hours.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        SESSION_COOKIE_NAME="__Host-session" if settings.secure_cookies else "session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=16 * 1024,  # forms here are tiny; reject big uploads
    )
    if settings.secure_cookies:
        # On Vercel, HTTPS ends at Vercel's proxy, which forwards plain HTTP and
        # says so in X-Forwarded-* headers. Trusting exactly one proxy hop lets the
        # app see requests as HTTPS, which turns on Flask-WTF's same-site check.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    logging.getLogger("werkzeug").addFilter(RedactQueryStrings())
    csrf = CSRFProtect(app)  # every POST must carry a token only our pages know
    init_db(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(tasks.bp)
    csrf.exempt(tasks.bp)  # called by the cron service with a secret token, not a browser form
    app.add_template_filter(when_text, "when")
    app.add_template_global(settings.auto_sync_schedule, "auto_sync_schedule")
    app.add_template_global(settings.site_verification, "site_verification")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Never send our URLs (which can carry OAuth codes) to other sites. Same-site
        # requests keep the Referer, which Flask-WTF checks on HTTPS form posts.
        response.headers["Referrer-Policy"] = "same-origin"
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
        user_id = find_user_id(db, email)
        if user_id is None:
            raise click.ClickException("No web user with that email. Sign in on the website first.")
        if load_state(db, user_id) is not None:
            raise click.ClickException("That account already has sync records; refusing to overwrite them.")
        state = SyncState.load()  # validates and migrates the file
        save_state(db, user_id, state.to_dict())
        click.echo(f"Imported {len(state.events)} event(s) and {len(state.tasks)} task(s).")

    @app.cli.command("sync-all")
    def sync_all() -> None:
        """Sync every user with automatic sync on (for cron or a scheduled job)."""
        results = scheduler.sync_all_users(app, trigger="cron")
        for r in results:
            click.echo(f"{r.email}: {'ok' if r.ok else 'FAILED - ' + (r.error or 'unknown error')}")
        if not results:
            click.echo("No users have automatic sync turned on with a feed and Google access saved.")
        if any(not r.ok for r in results):
            raise SystemExit(1)  # non-zero exit lets cron or the host flag the failure

    if start_scheduler:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        scheduler.start_scheduler(app, settings.sync_interval_hours)

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
