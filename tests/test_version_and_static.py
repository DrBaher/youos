"""Single-source version + shared static assets (UI PR A).

Version was hardcoded and drifted in three places (settings default,
/api/config, the UI footers). These pin that it resolves dynamically and that
the shared design-system assets are served and linked.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.version import get_version
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'(?m)^version = "([^"]+)"', text).group(1)


def test_get_version_matches_pyproject():
    assert get_version() == _pyproject_version()


def test_api_config_uses_dynamic_version():
    c = TestClient(app)
    body = c.get("/api/config").json()
    assert body["version"] == get_version()
    assert body["version"] != "0.1.10"  # the old hardcoded value


def test_static_assets_served():
    c = TestClient(app)
    css = c.get("/static/youos.css")
    js = c.get("/static/youos.js")
    assert css.status_code == 200 and "--teal" in css.text
    assert js.status_code == 200 and "hydrateChrome" in js.text


def test_chrome_pages_link_shared_assets_and_version_target():
    c = TestClient(app)
    for path in ("/stats", "/feedback", "/about", "/bookmarklet"):
        body = c.get(path).text
        assert "/static/youos.css" in body, f"{path} missing shared stylesheet"
        assert 'id="appVersion"' in body, f"{path} missing version hydration target"
        assert "YouOS v0.1.10" not in body, f"{path} still has hardcoded version"


def test_gmail_page_promotes_extension_with_install_steps():
    """The /bookmarklet page leads with the extension + concrete install steps,
    and injects the real extension/ folder path for 'Load unpacked'."""
    body = TestClient(app).get("/bookmarklet").text
    assert "Install the extension" in body
    assert "Load unpacked" in body and "Developer mode" in body
    assert "chrome://extensions" in body
    # The placeholder is substituted with the actual on-disk extension folder.
    assert "YOUOS_EXTENSION_PATH" not in body
    assert body.count("/extension</div>") >= 1 or "/extension<" in body
    # Bookmarklet demoted to a fallback, not the headline.
    assert "No-install fallback" in body
    # Nav relabeled across the app.
    assert ">Gmail</a>" in body
