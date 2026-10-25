"""Public pages that don't need sign-in."""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("public", __name__)


@bp.get("/privacy")
def privacy():
    return render_template("privacy.html")
