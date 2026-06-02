"""b190 — sender-type classification enrichment.

Covers:
  (i)   sender_profiles lookup → enriched type/company for a known sender
  (ii)  broadened automated detection (noreply/notifications/bounce/mailer-daemon)
  (iii) role mailbox info@/support@ NOT hard-classified automated (soft)
  (iv)  free-mail → personal, internal domain → internal
  (v)   extracted-but-unmatched email → external_client, NOT unknown
  (vi)  unknown ONLY when no parseable email
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.sender import classify_sender, classify_sender_detail


def _make_profiles_db(tmp_path, rows):
    """Create a minimal sender_profiles DB; return a sqlite:/// database_url."""
    db = tmp_path / "profiles.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE sender_profiles (
            id INTEGER PRIMARY KEY, email TEXT, display_name TEXT,
            domain TEXT, company TEXT, sender_type TEXT,
            relationship_note TEXT, reply_count INTEGER,
            avg_reply_words REAL, avg_response_hours REAL,
            first_seen TEXT, last_seen TEXT, topics_json TEXT,
            updated_at TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO sender_profiles (email, domain, company, sender_type, relationship_note, reply_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


# ── (i) profile lookup ──────────────────────────────────────────────────────


def test_profile_email_lookup_wins(tmp_path):
    db = _make_profiles_db(
        tmp_path,
        [("vip@bigclient.com", "bigclient.com", "BigClient Inc", "external_client", "Key account", 42)],
    )
    detail = classify_sender_detail("VIP <vip@bigclient.com>", db)
    assert detail.sender_type == "external_client"
    assert detail.source == "profile_email"
    assert detail.company == "BigClient Inc"
    assert detail.relationship_note == "Key account"


def test_profile_domain_fallback(tmp_path):
    # No exact-email row, but the domain has a profiled correspondent.
    db = _make_profiles_db(
        tmp_path,
        [("known@partner.com", "partner.com", "Partner Co", "external_client", None, 10)],
    )
    detail = classify_sender_detail("someone-else@partner.com", db)
    assert detail.sender_type == "external_client"
    assert detail.source == "profile_domain"
    assert detail.company == "Partner Co"


def test_profile_overrides_heuristic(tmp_path):
    # Heuristic would call ``billing@`` automated; an enriching profile that
    # says external_client (a real human at billing@) wins.
    db = _make_profiles_db(
        tmp_path,
        [("billing@smallco.com", "smallco.com", "SmallCo", "external_client", "Real person", 5)],
    )
    assert classify_sender("billing@smallco.com") == "automated"  # heuristic-only
    assert classify_sender("billing@smallco.com", db) == "external_client"  # profile wins


def test_profile_unknown_type_is_ignored(tmp_path):
    # A stale profile storing 'unknown' must NOT pin a live sender to unknown;
    # fall back to heuristics.
    db = _make_profiles_db(
        tmp_path,
        [("x@randomcorp.io", "randomcorp.io", None, "unknown", None, 1)],
    )
    assert classify_sender("x@randomcorp.io", db) == "external_client"


def test_no_database_url_is_pure_heuristic():
    # Backward-compatible single-arg form: no profile lookup.
    assert classify_sender("ismael@launch.co") == "external_client"


# ── (ii) broadened automated detection ──────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [
        "noreply@company.com",
        "no-reply@company.com",
        "no_reply@company.com",
        "donotreply@company.com",
        "notifications@github.com",
        "notification@x.com",
        "bounce@sendgrid.net",
        "bounces+tag@sendgrid.net",
        "MAILER-DAEMON@mail.example.com",
        "mailer@brand.com",
        "newsletter@brand.com",
        "marketing@brand.com",
        "alerts@status.io",
        "updates@product.com",
        "postmaster@host.com",
        "automated@svc.com",
    ],
)
def test_broadened_automated_detection(addr):
    assert classify_sender(addr) == "automated"


# ── (iii) role mailbox is a SOFT signal, not hard automated ─────────────────


@pytest.mark.parametrize("addr", ["info@smallco.com", "support@smallco.com", "sales@smallco.com", "hello@studio.com"])
def test_role_mailbox_not_hard_automated(addr):
    detail = classify_sender_detail(addr)
    assert detail.sender_type == "external_client"
    assert detail.sender_type != "automated"
    assert "role mailbox" in detail.reason


# ── (iv) free-mail → personal, internal domain → internal ───────────────────


def test_free_mail_is_personal():
    assert classify_sender("alice@gmail.com") == "personal"
    assert classify_sender("bob@outlook.com") == "personal"


def test_internal_domain_is_internal(monkeypatch):
    monkeypatch.setattr("app.core.sender.get_internal_domains", lambda: {"work.example"})
    assert classify_sender("baher@work.example") == "internal"


def test_internal_overrides_role_mailbox_but_automated_wins(monkeypatch):
    monkeypatch.setattr("app.core.sender.get_internal_domains", lambda: {"work.example"})
    # role mailbox at an internal domain → internal (human colleague)
    assert classify_sender("info@work.example") == "internal"
    # but a hard automated local at an internal domain is still machine mail
    assert classify_sender("noreply@work.example") == "automated"


# ── (v) extracted-but-unmatched email → external_client, NOT unknown ────────


def test_extracted_unmatched_is_external_client_not_unknown():
    assert classify_sender("jane@some-unknown-corp.io") == "external_client"
    assert classify_sender("Jane Doe <jane@some-unknown-corp.io>") == "external_client"


# ── (vi) unknown ONLY when no parseable email ───────────────────────────────


def test_unknown_only_for_no_parseable_email():
    assert classify_sender(None) == "unknown"
    assert classify_sender("") == "unknown"
    assert classify_sender("not an email at all") == "unknown"
    # malformed multi-@ addr-spec (rejected by hardening) → no parseable addr
    assert classify_sender("Name <a@b@c.com>") == "unknown"


def test_detail_reason_for_unknown():
    detail = classify_sender_detail("no email here")
    assert detail.sender_type == "unknown"
    assert detail.source == "none"
    assert "no parseable" in detail.reason


def test_missing_profiles_table_falls_back_to_heuristic(tmp_path):
    # database_url pointing at a DB without a sender_profiles table must not
    # raise — classification falls back to heuristics.
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    url = f"sqlite:///{db}"
    assert classify_sender("jane@corp.io", url) == "external_client"
