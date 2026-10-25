"""Public pages that don't need sign-in."""

from __future__ import annotations

from flask import Blueprint, Response, render_template

bp = Blueprint("public", __name__)

# Google Search Console's ownership check for https://canvas2google.vercel.app/.
# The file name and its contents are public by design; they only show Google
# that whoever controls this site also controls the Search Console account.
GOOGLE_VERIFICATION_FILE = "google95e308e5f9dbfb39.html"


@bp.get("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.get(f"/{GOOGLE_VERIFICATION_FILE}")
def google_site_verification():
    return Response(f"google-site-verification: {GOOGLE_VERIFICATION_FILE}", mimetype="text/html")
