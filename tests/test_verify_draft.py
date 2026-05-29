"""Tests for verify-before-accept (Phase A3).

Deterministic safety checks on a generated draft: language match and no
invented email / link / amount. Blocking issues should hold a draft for human
review (they collapse the quality score upstream).
"""

from __future__ import annotations

from app.generation.verify import verify_draft

# --- clean drafts ----------------------------------------------------------


def test_clean_draft_passes():
    r = verify_draft(
        "Hi Alice — Q3 pricing is unchanged. Talk soon.",
        inbound="Could you confirm the Q3 pricing?",
    )
    assert r.ok
    assert r.blocking == []


def test_quoting_an_email_from_the_inbound_is_allowed():
    r = verify_draft(
        "Sure, I'll loop in support@acme.com as you asked.",
        inbound="Please CC support@acme.com on the reply.",
    )
    assert r.ok


def test_participant_emails_are_allowed():
    # The account + sender addresses are participants, fine to mention.
    r = verify_draft(
        "I'll reply from me@myco.com — talk soon.",
        inbound="Quick question.",
        account_email="me@myco.com",
        sender="Bob <bob@partner.com>",
    )
    assert r.ok


# --- blocking issues -------------------------------------------------------


def test_invented_email_blocks():
    r = verify_draft(
        "Sure — email me at totally-made-up@elsewhere.com.",
        inbound="Can you help?",
    )
    assert not r.ok
    assert any("invented email" in b for b in r.blocking)


def test_invented_link_blocks():
    r = verify_draft(
        "See the details at https://fake.example.com/promo.",
        inbound="What are the details?",
    )
    assert not r.ok
    assert any("invented link" in b for b in r.blocking)


def test_link_present_in_inbound_is_allowed():
    r = verify_draft(
        "Yes, https://acme.com/doc looks right.",
        inbound="Is https://acme.com/doc the latest?",
    )
    assert r.ok


def test_language_mismatch_blocks():
    # German inbound, English draft → mismatch.
    r = verify_draft(
        "Thanks for your order, it will ship soon.",
        inbound="Sehr geehrte Damen und Herren, bitte bestätigen Sie die Bestellung mit freundlichen Grüßen.",
    )
    assert not r.ok
    assert any("language mismatch" in b for b in r.blocking)


# --- warnings (non-blocking) -----------------------------------------------


def test_invented_amount_warns_not_blocks():
    r = verify_draft(
        "The total comes to $4,250 as discussed.",
        inbound="What's the total?",
    )
    assert r.ok  # warning only — not blocking
    assert any("amount" in w for w in r.warnings)


def test_proposed_time_warns_not_blocks():
    r = verify_draft(
        "How about we meet at 3:30pm on Thursday?",
        inbound="Can we find a time to talk?",
    )
    assert r.ok
    assert any("time/date" in w for w in r.warnings)


def test_thread_history_counts_as_grounding():
    r = verify_draft(
        "As I said, reach me at me@myco.com.",
        inbound="Remind me of your email?",
        thread_history=[{"sender": "me", "text": "you can reach me at me@myco.com"}],
    )
    assert r.ok


def test_issues_property_prefixes_severity():
    r = verify_draft(
        "Email made-up@x.com; total is $99.",
        inbound="hi",
    )
    assert any(s.startswith("[block]") for s in r.issues)
    assert any(s.startswith("[warn]") for s in r.issues)
