"""Endpoint the cloud cron service calls to run the scheduled sync.

Vercel can't keep a background thread alive, so its cron service calls this URL
once a day. It's protected by a shared secret: Vercel sends
`Authorization: Bearer <CRON_SECRET>` automatically when that variable is set.
"""

from __future__ import annotations

import hmac

from flask import Blueprint, abort, current_app, jsonify, request

from . import scheduler

bp = Blueprint("tasks", __name__)


@bp.route("/tasks/sync-all", methods=["GET", "POST"])
def sync_all():
    secret = current_app.config["SETTINGS"].cron_secret
    if not secret:
        abort(404)  # endpoint switched off unless a secret is configured
    supplied = request.headers.get("Authorization", "")
    # compare_digest takes the same time whether the first or last character is
    # wrong, so response timing can't be used to guess the secret.
    if not hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode()):
        current_app.logger.warning("Rejected an unauthorized call to /tasks/sync-all")
        abort(401)

    results = scheduler.sync_all_users(current_app._get_current_object(), trigger="cron")
    failed = sum(not r.ok for r in results)
    # Counts only; no emails or error details in the response.
    return jsonify(users=len(results), failed=failed), (500 if failed else 200)
