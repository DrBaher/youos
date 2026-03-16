"""Tests for per-account corpus isolation (Item 4)."""

from __future__ import annotations

from app.core.config import get_account_for_sender


def test_internal_sender_returns_work_email(monkeypatch):
    """Sender from internal domain → work account email."""
    config = {
        "user": {
            "emails": ["alice@acme.com", "alice@gmail.com"],
            "internal_domains": ["acme.com"],
        }
    }
    result = get_account_for_sender("bob@acme.com", config=config)
    assert result == "alice@acme.com"


def test_personal_sender_returns_personal_email(monkeypatch):
    """Sender from gmail → personal account email."""
    config = {
        "user": {
            "emails": ["alice@acme.com", "alice@gmail.com"],
            "internal_domains": ["acme.com"],
        }
    }
    result = get_account_for_sender("friend@gmail.com", config=config)
    assert result == "alice@gmail.com"


def test_external_sender_returns_none():
    """Sender from unknown domain → None (no filter)."""
    config = {
        "user": {
            "emails": ["alice@acme.com", "alice@gmail.com"],
            "internal_domains": ["acme.com"],
        }
    }
    result = get_account_for_sender("client@bigcorp.com", config=config)
    assert result is None


def test_no_sender_returns_none():
    """No sender → None."""
    result = get_account_for_sender("", config={"user": {"emails": ["a@b.com"]}})
    assert result is None


def test_no_at_in_sender_returns_none():
    """Sender without @ → None."""
    result = get_account_for_sender("noatsign", config={"user": {"emails": ["a@b.com"]}})
    assert result is None


def test_no_emails_configured():
    """No user emails configured → None."""
    result = get_account_for_sender("bob@acme.com", config={"user": {}})
    assert result is None


def test_internal_sender_falls_back_to_first_email():
    """If no non-personal email exists, fall back to first email."""
    config = {
        "user": {
            "emails": ["alice@gmail.com"],
            "internal_domains": ["acme.com"],
        }
    }
    result = get_account_for_sender("bob@acme.com", config=config)
    assert result == "alice@gmail.com"


def test_yahoo_is_personal():
    """Yahoo is treated as personal domain."""
    config = {
        "user": {
            "emails": ["work@company.com", "me@yahoo.com"],
            "internal_domains": ["company.com"],
        }
    }
    result = get_account_for_sender("friend@yahoo.com", config=config)
    assert result == "me@yahoo.com"
