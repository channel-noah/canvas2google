"""Sign-in and sign-out routes, and the login_required guard."""

from __future__ import annotations

import functools
import hmac

from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for

from . import limits, oauth
from .crypto import secret_context
from .db import count_users, get_db, get_secret, get_user, set_secret, upsert_user, user_exists

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


def _revoke_quietly(creds) -> None:
    """Best effort: a failed revoke must never turn a refusal into a crash."""
    token = getattr(creds, "refresh_token", None) or getattr(creds, "token", None)
    if token and not oauth.revoke(token):
        current_app.logger.warning("Couldn't revoke Google access for a refused sign-in")


def _refusal_reason(settings, db, identity: dict, email: str) -> tuple[str, str] | None:
    """Why this Google account may not use the site, as (log reason, user message), or None."""
    if not identity.get("email_verified"):
        return "unverified email", "This Google account's email isn't verified."
    if settings.signup_mode == "invite" and email not in settings.allowed_emails:
        return "not invited", "This Google account isn't allowed to use this site."
    # Existing users can always sign back in; only new accounts count toward the cap.
    if not user_exists(db, identity["sub"]) and count_users(db) >= settings.max_users:
        return "user cap reached", "Sorry, this site isn't accepting new accounts right now."
    return None


def _fail(message: str, status: int = 400):
    return render_template("error.html", message=message), status


@bp.get("/login")
def login():
    return render_template("login.html")


@bp.get("/auth/google")
def start():
    settings = current_app.config["SETTINGS"]
    if limits.hit(get_db(), limits.SIGN_IN, limits.ip_label()):
        return limits.too_many(limits.SIGN_IN)
    url, state, verifier = oauth.authorization_url(settings, force_consent=request.args.get("consent") == "1")
    # Remembered in the (signed) session cookie and checked on the way back.
    session["oauth_state"] = state
    session["oauth_verifier"] = verifier
    return redirect(url)


@bp.get("/auth/callback")
def callback():
    settings = current_app.config["SETTINGS"]
    if limits.hit(get_db(), limits.SIGN_IN, limits.ip_label()):
        return limits.too_many(limits.SIGN_IN)
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
    db = get_db()
    # Decide whether this account may use the site *before* storing anything.
    refusal = _refusal_reason(settings, db, identity, email)
    if refusal:
        current_app.logger.warning("Sign-in refused (%s)", refusal[0])
        # They just granted this app access in Google; give it back, so it doesn't
        # linger in their account. (Not done for allowed users below: revoking any
        # token cancels the whole grant, including a token we already rely on.)
        _revoke_quietly(creds)
        return _fail(refusal[1], 403)
    if not oauth.has_sync_scopes(creds):
        return _fail("Calendar and Tasks access are both needed. Please sign in again and allow both.")

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
