"""Web Settings page (Config PR 3).

/settings renders the whitelisted flags as toggles backed by the config-write
API. Verified structurally (serves + wired); visual behavior is eyeballed on a
running instance.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_settings_page_serves_and_is_wired():
    body = client.get("/settings").text
    assert "/api/config/flags" in body          # reads the flag list
    assert "/api/config/set" in body            # writes via the API
    assert 'id="flags"' in body                 # render target
    assert "function controlFor" in body or "controlFor" in body  # toggle/select builder


def test_settings_in_nav_across_chrome():
    for path in ("/feedback", "/stats", "/about", "/bookmarklet", "/settings"):
        assert 'href="/settings"' in client.get(path).text, f"{path} missing Settings nav link"


def test_settings_links_shared_assets():
    body = client.get("/settings").text
    assert "/static/youos.css" in body
    assert 'id="appVersion"' in body
