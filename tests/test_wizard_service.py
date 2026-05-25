"""Install the background service from the wizard.

POST /api/service/install runs the launchd install; GET /api/service/status
reports it. The "Keep it running" wizard step calls these. service.install is
mocked so tests never install a real LaunchAgent.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import service
from app.main import app

client = TestClient(app)


def test_service_install_endpoint_ok(monkeypatch):
    monkeypatch.setattr(service, "install", lambda: (True, "Installed and started."))
    r = client.post("/api/service/install")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Installed and started."}


def test_service_install_endpoint_failure_is_500(monkeypatch):
    monkeypatch.setattr(service, "install", lambda: (False, "launchctl boom"))
    r = client.post("/api/service/install")
    assert r.status_code == 500
    assert "launchctl boom" in r.json()["detail"]


def test_service_status_endpoint(monkeypatch):
    monkeypatch.setattr(service, "status", lambda: "running (LaunchAgent loaded)")
    assert client.get("/api/service/status").json() == {"status": "running (LaunchAgent loaded)"}


def test_wizard_has_keep_it_running_step():
    body = client.get("/welcome").text
    assert 'id="installService"' in body
    assert "/api/service/install" in body
    assert "/api/service/status" in body
    assert "Keep it running" in body
    assert 'data-step="7"' in body  # Done shifted to step 8 (index 7)
