"""Rate limits, stored in the database.

Vercel runs each request in a short-lived instance with no shared memory, so
in-memory counters wouldn't work; counts live in the `rate_limits` table.

IP addresses are never stored. A keyed hash (HMAC with the app's secret key)
turns each IP into a stable, unreadable label, so repeat visits can be counted
without keeping anyone's address.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from flask import current_app, render_template, request
from sqlalchemy import Connection

from .db import count_hit, current_hits


@dataclass(frozen=True)
class Limit:
    name: str
    max_hits: int
    window_seconds: int


SIGN_IN = Limit("signin", 10, 600)  # sign-in starts + callbacks, per IP
SYNC = Limit("sync", 6, 600)  # Sync now / Preview, per user
FEED_FETCH = Limit("feed", 30, 600)  # dashboard Canvas fetches, per user
CRON_FAILURES = Limit("cronfail", 5, 600)  # wrong cron secrets, per IP


def ip_label() -> str:
    """A keyed hash of the client's IP: countable, but not reversible to an address."""
    ip = request.remote_addr or "unknown"
    secret = current_app.config["SECRET_KEY"].encode()
    return hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest()[:24]


def _key(limit: Limit, subject: str) -> str:
    return f"{limit.name}:{subject}"


def hit(db: Connection, limit: Limit, subject: str) -> bool:
    """Count one hit. Returns True if this request is over the limit."""
    return count_hit(db, _key(limit, subject), limit.window_seconds) > limit.max_hits


def already_over(db: Connection, limit: Limit, subject: str) -> bool:
    """Check without counting (for limits that only count failures)."""
    return current_hits(db, _key(limit, subject), limit.window_seconds) >= limit.max_hits


def too_many(limit: Limit):
    response = current_app.make_response(
        (render_template("error.html", message="Too many requests. Please wait a few minutes and try again."), 429)
    )
    response.headers["Retry-After"] = str(limit.window_seconds)
    return response
