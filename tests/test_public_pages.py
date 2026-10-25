"""Public pages: landing, terms and privacy are reachable without signing in."""

import pytest


def text(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


def test_signed_out_visitors_to_root_reach_the_landing_page(client):
    response = client.get("/", follow_redirects=True)
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "Sign in with Google" in page and "How it works" in page


def test_landing_page_explains_every_permission(client):
    page = text(client, "/login")
    # Google's reviewers expect the home page to explain the app and its data use.
    for phrase in ("Your email address", "Calendars this app creates", "Google Tasks", "can't see"):
        assert phrase in page, phrase
    assert "invited accounts" in page  # invite-only note in the default mode


def test_landing_page_links_to_policies_and_source(client):
    page = text(client, "/login")
    for link in ('href="/privacy"', 'href="/terms"', "https://github.com/channel-noah/canvas2google"):
        assert link in page, link


def test_landing_screenshot_is_served_from_our_own_site(client):
    page = text(client, "/login")
    assert 'src="/static/dashboard-demo.jpg"' in page  # CSP only allows same-site images
    image = client.get("/static/dashboard-demo.jpg")
    assert image.status_code == 200 and image.mimetype == "image/jpeg"


def test_terms_page(client):
    page = text(client, "/terms")
    assert "as is" in page and "Always check due dates in Canvas" in page
    assert "Delete my account" in page and 'href="/privacy"' in page


def test_privacy_page_covers_retention_providers_and_children(client):
    page = text(client, "/privacy")
    for phrase in ("20 most recent", "never stored", "Vercel", "Neon", "under 13", "Limited Use", 'href="/terms"'):
        assert phrase in page, phrase


@pytest.mark.parametrize("path", ["/login", "/privacy", "/terms"])
def test_every_public_page_links_to_privacy_and_terms(client, path):
    page = text(client, path)
    assert 'href="/privacy"' in page and 'href="/terms"' in page


def test_footer_links_also_appear_for_signed_in_users(logged_in):
    page = text(logged_in, "/settings")
    assert 'href="/privacy"' in page and 'href="/terms"' in page
