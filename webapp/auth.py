"""Sign-in and sign-out routes, and the login_required guard."""

from __future__ import annotations

import functools
import hmac

from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for

from . import oauth
from .crypto import secret_context
from .db import get_db, get_secret, get_user, set_secret, upsert_user

bp = Blueprint("auth", __name__)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        g.user = get_user(get_db(), user_id) if user_id else None
        if g.user is None:
            session.clear()
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def _fail(message: str, status: int = 400):
    return render_template("error.html", message=message), status


@bp.get("/login")
def login():
    return render_template("login.html")


@bp.get("/auth/google")
def start():
    settings = current_app.config["SETTINGS"]
    url, state, verifier = oauth.authorization_url(settings, force_consent=request.args.get("consent") == "1")
    # Remembered in the (signed) session cookie and checked on the way back.
    session["oauth_state"] = state
    session["oauth_verifier"] = verifier
    return redirect(url)


@bp.get("/auth/callback")
def callback():
    settings = current_app.config["SETTINGS"]
    expected_state = session.pop("oauth_state", None)
    verifier = session.pop("oauth_verifier", None)

    # The state must match what we stored, which proves this sign-in started in
    # this browser. That blocks "login CSRF" (being signed into someone else's account).
    if not expected_state or not verifier or not hmac.compare_digest(request.args.get("state", ""), expected_state):
        return _fail("This sign-in link expired or didn't start here. Please try again.")
    if "error" in request.args or not request.args.get("code"):
        return _fail("Google sign-in was cancelled or didn't complete.")

    try:
        creds = oauth.exchange_code(settings, request.args["code"], expected_state, verifier)
        identity = oauth.verify_identity(settings, creds)
    except Exception as exc:  # OAuth, token-verification and network errors
        current_app.logger.warning("Sign-in failed (%s)", type(exc).__name__)
        return _fail("Google sign-in didn't complete. Please try again.")

    email = str(identity.get("email", "")).lower()
    # Check the allowlist before storing anything about this account.
    if not identity.get("email_verified") or email not in settings.allowed_emails:
        current_app.logger.warning("Sign-in refused for an account not on the allowlist")
        return _fail("This Google account isn't allowed to use this site.", 403)
    if not oauth.has_sync_scopes(creds):
        return _fail("Calendar and Tasks access are both needed. Please sign in again and allow both.")

    db = get_db()
    user_id = upsert_user(db, identity["sub"], email)
    box = current_app.config["SECRET_BOX"]
    if creds.refresh_token:
        set_secret(db, user_id, "google_token", box.encrypt(creds.to_json(), secret_context(user_id, "google_token")))
    elif get_secret(db, user_id, "google_token") is None:
        # Google only sends a refresh token on first consent; ask again to get one.
        return redirect(url_for("auth.start", consent="1"))

    # Start a brand-new session on sign-in (prevents session fixation).
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    return redirect(url_for("views.dashboard"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
