"""Gmail push-notification webhook (b282).

The endpoint is public (Pub/Sub can't send a PIN), so the tests pin: inert unless
configured, constant-time shared-secret auth, Pub/Sub envelope decode, account
matching, and that a valid push schedules a (mocked) background sweep.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import gmail_push_routes as gp


def _envelope(email: str | None) -> dict:
    inner = {"historyId": "9"}
    if email is not None:
        inner["emailAddress"] = email
    data = base64.b64encode(json.dumps(inner).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "projects/x/subscriptions/y"}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(gp.router)
    return TestClient(app)


# --- unit: decode / match --------------------------------------------------


def test_decode_email_valid_malformed_and_missing():
    assert gp._decode_email(_envelope("a@b.com")) == "a@b.com"
    assert gp._decode_email(_envelope(None)) is None  # no emailAddress key
    assert gp._decode_email({"message": {"data": "!!not-base64"}}) is None
    assert gp._decode_email({}) is None


def test_match_account_case_insensitive(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_accounts", lambda *a, **k: ("Me@X.com", "two@x.com"))
    assert gp._match_account("me@x.com") == "Me@X.com"
    assert gp._match_account("stranger@z.com") is None
    assert gp._match_account("") is None


# --- endpoint --------------------------------------------------------------


def test_push_inert_until_enabled(client, monkeypatch):
    monkeypatch.setattr(gp, "_push_config", lambda: {"enabled": False, "token": ""})
    r = client.post("/api/gmail/push?token=whatever", json=_envelope("a@b.com"))
    assert r.status_code == 404


def test_push_rejects_bad_token(client, monkeypatch):
    monkeypatch.setattr(gp, "_push_config", lambda: {"enabled": True, "token": "right-secret"})
    r = client.post("/api/gmail/push?token=wrong", json=_envelope("a@b.com"))
    assert r.status_code == 403


def test_push_good_token_known_account_schedules_sweep(client, monkeypatch):
    monkeypatch.setattr(gp, "_push_config", lambda: {"enabled": True, "token": "s3cret"})
    monkeypatch.setattr("app.core.config.get_ingestion_accounts", lambda *a, **k: ("me@x.com",))
    swept: list[str] = []
    monkeypatch.setattr(gp, "_sweep_account", lambda acct: swept.append(acct))
    r = client.post("/api/gmail/push?token=s3cret", json=_envelope("me@x.com"))
    assert r.status_code == 200
    assert r.json()["account"] == "me@x.com"
    assert swept == ["me@x.com"]  # the background task ran


def test_push_unknown_account_is_acked_noop(client, monkeypatch):
    monkeypatch.setattr(gp, "_push_config", lambda: {"enabled": True, "token": "s3cret"})
    monkeypatch.setattr("app.core.config.get_ingestion_accounts", lambda *a, **k: ("me@x.com",))
    swept: list[str] = []
    monkeypatch.setattr(gp, "_sweep_account", lambda acct: swept.append(acct))
    r = client.post("/api/gmail/push?token=s3cret", json=_envelope("stranger@z.com"))
    assert r.status_code == 200
    assert "ignored" in r.json()
    assert swept == []  # no sweep for a mailbox we don't triage


def test_push_malformed_notification_is_acked_noop(client, monkeypatch):
    """A garbage message is acked (200) so Pub/Sub stops retrying it."""
    monkeypatch.setattr(gp, "_push_config", lambda: {"enabled": True, "token": "s3cret"})
    r = client.post("/api/gmail/push?token=s3cret", json={"message": {"data": "!!bad"}})
    assert r.status_code == 200
    assert "ignored" in r.json()
