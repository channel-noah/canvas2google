"""Dashboard, settings and sync pages."""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from canvas_sync.config import ConfigError, validate_feed_url
from canvas_sync.feed import FeedError, fetch_feed, parse_feed, upcoming

from . import sync_runner
from .auth import login_required
from .crypto import SecretError, secret_context
from .db import get_db, recent_runs, set_secret, update_preferences

bp = Blueprint("views", __name__)

# Offered in Settings. Kept to a short list (rather than every IANA zone) so the
# form only ever accepts known-good values.
TIMEZONES = [
    ("America/New_York", "Eastern (New York)"),
    ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"),
    ("America/Phoenix", "Arizona (Phoenix)"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"),
    ("America/Anchorage", "Alaska (Anchorage)"),
    ("Pacific/Honolulu", "Hawaii (Honolulu)"),
    ("UTC", "UTC"),
]


def _feed_url_or_none() -> tuple[str | None, str | None]:
    """Return (feed URL, problem message)."""
    try:
        return sync_runner.load_feed_url(get_db(), current_app.config["SECRET_BOX"], g.user["id"]), None
    except SecretError as exc:
        return None, str(exc)


@bp.get("/")
@login_required
def dashboard():
    tz = sync_runner.user_timezone(get_db(), g.user["id"])
    feed_url, problem = _feed_url_or_none()
    items = []
    if feed_url:
        try:
            items = upcoming(parse_feed(fetch_feed(feed_url)), tz=tz)
        except FeedError as exc:
            problem = str(exc)
    runs = recent_runs(get_db(), g.user["id"])
    return render_template(
        "dashboard.html",
        has_feed=bool(feed_url),
        feed_host=urlsplit(feed_url).hostname if feed_url else None,
        items=items[:30],
        tz=tz,
        problem=problem,
        runs=runs,
        last_failed=runs[0] if runs and not runs[0]["ok"] else None,
    )


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        try:
            url = validate_feed_url(request.form.get("feed_url", ""))
        except ConfigError as exc:
            flash(str(exc), "error")
        else:
            box = current_app.config["SECRET_BOX"]
            user_id = g.user["id"]
            set_secret(get_db(), user_id, "feed_url", box.encrypt(url, secret_context(user_id, "feed_url")))
            flash("Feed URL saved (encrypted).", "ok")
        # Redirect after POST, so refreshing the page doesn't resubmit the form.
        return redirect(url_for("views.settings_page"))

    feed_url, problem = _feed_url_or_none()
    # The full URL is never sent back to the browser, only its host.
    return render_template(
        "settings.html",
        feed_host=urlsplit(feed_url).hostname if feed_url else None,
        problem=problem,
        timezones=TIMEZONES,
    )


@bp.post("/settings/preferences")
@login_required
def save_preferences():
    timezone = request.form.get("timezone", "")
    if timezone not in dict(TIMEZONES):
        flash("Please choose a time zone from the list.", "error")
    else:
        update_preferences(get_db(), g.user["id"], timezone, request.form.get("auto_sync") == "on")
        flash("Preferences saved.", "ok")
    return redirect(url_for("views.settings_page"))


@bp.post("/sync")
@login_required
def sync():
    dry_run = request.form.get("mode") != "sync"
    outcome = sync_runner.run_sync(get_db(), current_app.config["SECRET_BOX"], g.user["id"], dry_run)
    return render_template("results.html", outcome=outcome)
