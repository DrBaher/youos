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


# --- b132: ReDoS bound on attacker inbound + API body-size caps -------------


def test_verify_draft_bounded_on_huge_no_at_inbound():
    """A long no-'@' inbound made _EMAIL_RE backtrack O(n^2) (50k≈2s, ~1MB≈13min),
    stalling the unattended sweep / pinning the /draft worker. The RFC-64 local-
    part bound + _MAX_VERIFY_CHARS cap make it linear and bounded."""
    import time

    payload = "x" * 1_000_000
    t0 = time.perf_counter()
    verify_draft("ok thanks", inbound="Can you confirm? " + payload)
    assert time.perf_counter() - t0 < 1.0  # generous ceiling for slow CI


def test_verify_grounding_semantics_unchanged_after_cap():
    """The regex/cap change must not regress the grounding checks."""
    assert any("invented email" in b
               for b in verify_draft("mail made-up@evil.com", inbound="hi").blocking)
    assert not any("invented email" in b
                   for b in verify_draft("to real@known.com", inbound="cc real@known.com please").blocking)
    assert any("invented link" in b
               for b in verify_draft("see https://evil.com/x", inbound="hi").blocking)
    assert not any("invented link" in b
                   for b in verify_draft("see https://x.com/a", inbound="visit https://x.com/a").blocking)


def test_draft_api_rejects_oversize_body():
    """A single request can't submit an unbounded body to the generation paths."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    big = "x" * 50_001
    assert client.post("/draft", json={"inbound_message": big}).status_code == 422
    assert client.post("/draft/compare", json={"inbound_text": big}).status_code == 422


def test_draft_compare_does_not_leak_exception_text(monkeypatch):
    """b136: a generation failure must return a static message, not raw
    exception text (which can carry filesystem paths / config keys)."""
    import json

    from fastapi.testclient import TestClient

    import app.api.routes as routes
    from app.main import app

    def _boom(*a, **k):
        raise RuntimeError("secret path /Users/x/.config/leak")

    monkeypatch.setattr(routes, "generate_draft", _boom)
    r = TestClient(app).post("/draft/compare", json={"inbound_text": "hi"})
    assert r.status_code == 200
    blob = json.dumps(r.json())
    assert "see server logs" in blob
    assert "secret path" not in blob  # raw exception not echoed


# --- b138: the attacker-controlled `sender` field must be length-bounded -----


def test_draft_sender_field_is_length_bounded():
    """b132 capped inbound_message/inbound_text but not `sender`; lookup_facts
    runs an O(n^2) email regex on it, so an 80 KB no-'@' sender hung the worker."""
    import pytest
    from pydantic import ValidationError

    from app.api.routes import DraftBody, DraftCompareBody

    with pytest.raises(ValidationError):
        DraftBody(inbound_message="hi", sender="x" * 1025)
    with pytest.raises(ValidationError):
        DraftCompareBody(inbound_text="hi", sender="x" * 1025)
    # a normal "Name <email>" still validates.
    assert DraftBody(inbound_message="hi", sender="Alice <a@x.com>").sender == "Alice <a@x.com>"


def test_lookup_facts_bounded_on_huge_no_at_sender():
    import sqlite3
    import time

    from app.generation.service import lookup_facts

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE memory (id INTEGER PRIMARY KEY, type TEXT, key TEXT, fact TEXT, "
        "confidence REAL DEFAULT 0.8, tags TEXT DEFAULT '[]', "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    t0 = time.perf_counter()
    lookup_facts(sender="x" * 100_000, inbound_text="hi", database_url="sqlite:///:memory:", conn=conn)
    assert time.perf_counter() - t0 < 0.5  # was ~14s at 80k, longer at 100k


# --- ungrounded status assertions (b229) ------------------------------------
# Live no_send review (2026-06-11): the worst draft failures asserted completed
# states the thread contradicted ("June payment has been received" replying to
# a payment chaser; "Resignation filed with ADGM" when the sender said THEY
# would file it). These are collected in ``status_claims`` so the autonomous
# path can collapse quality on them; they stay warning-severity (ok=True).


def test_ungrounded_received_assertion_flagged():
    r = verify_draft(
        "Hi,\n\nJune payment has been received. No further action needed.",
        inbound="I wanted to check in on your outstanding June payment.",
    )
    assert r.ok  # warning severity, never blocking
    assert len(r.status_claims) == 2  # "has been received" + "no further action"
    assert any("received" in c.lower() for c in r.status_claims)
    assert any("asserts unverified status" in w for w in r.warnings)


def test_grounded_status_echo_not_flagged():
    # Echoing the sender's own wording is grounded — not a fabrication.
    r = verify_draft(
        "Thanks — glad the payment has been received and nothing further is needed.",
        inbound="Your payment has been received; no further action needed from you.",
    )
    assert r.status_claims == []


def test_filed_with_authority_assertion_flagged():
    r = verify_draft(
        "Received. Resignation filed with ADGM. Entity remains active.",
        inbound="Please find attached our resignation letter. We will file it today.",
    )
    assert any("filed" in c.lower() for c in r.status_claims)


def test_now_active_assertion_flagged():
    r = verify_draft(
        "Direct debit is now active and will streamline future payments.",
        inbound="We are introducing direct debit payments — would you like to opt in?",
    )
    assert any("active" in c.lower() for c in r.status_claims)


def test_plain_reply_has_no_status_claims():
    r = verify_draft(
        "Hi Alice — yes, Thursday 14:00 works for me. Talk soon.",
        inbound="Does Thursday 14:00 work for you?",
    )
    assert r.status_claims == []


# --- b286: leaked scaffolding / placeholders (blocking) -------------------


def test_leaked_facts_context_is_blocking():
    r = verify_draft(
        "Hi Leslie,\n\nMedicus is a strong fit.\n\n"
        "[FACTS CONTEXT] About you: Based in Dubai, active in healthtech.",
        inbound="Could Medicus be a fit for our fund?",
    )
    assert not r.ok
    assert any("scaffolding" in b.lower() for b in r.blocking)


def test_list_attached_placeholder_is_blocking():
    r = verify_draft(
        "Hi Theresa — slides updated. Members without access: [list attached].",
        inbound="Please send a list of people who need access.",
    )
    assert not r.ok
    assert any("placeholder" in b.lower() for b in r.blocking)


# --- b286: invented personal/family detail (fabrication) -----------------


def test_invented_family_detail_flagged():
    r = verify_draft(
        "Hi Christopher — happy to connect! Wishing you a safe trip — "
        "new baby's arrival is a lot of energy!",
        inbound="Great to connect. I'm on vacation until the second week of July.",
    )
    assert any("family" in f.lower() or "personal" in f.lower() for f in r.fabrications)


def test_grounded_family_detail_not_flagged():
    # The sender raised the baby first — a warm acknowledgement is correct.
    r = verify_draft(
        "Congrats on the new baby! Let's reconnect once you're settled.",
        inbound="I'll be on paternity leave — our baby arrived last week!",
    )
    assert r.fabrications == []


def test_reason_does_not_ground_family_via_son_substring():
    # "reason" must NOT count as the family stem "son" (word-boundary check).
    r = verify_draft(
        "Thanks — our daughter's birthday kept me busy, sorry for the delay.",
        inbound="For that reason, could you send the report this week?",
    )
    assert any("family" in f.lower() or "personal" in f.lower() for f in r.fabrications)


# --- b286: hallucinated review meeting (fabrication) ---------------------


def test_hallucinated_review_meeting_flagged():
    r = verify_draft(
        "Thanks for the note. We'll cover this in tomorrow's full review meeting.",
        inbound="Do we have a date for the call yet?",
    )
    assert any("review meeting" in f.lower() for f in r.fabrications)


def test_grounded_review_meeting_not_flagged():
    r = verify_draft(
        "Yes — let's discuss it at the review meeting on Thursday.",
        inbound="Can we go over the numbers at the review meeting this week?",
    )
    assert all("review meeting" not in f.lower() for f in r.fabrications)


# --- b286: speaker inversion (fabrication) -------------------------------


def test_addressed_to_the_user_is_inversion():
    r = verify_draft(
        "Sehr geehrter Herr Hakim,\n\nwir koennen das leider nicht unterstuetzen.\n\n"
        "Viele Gruesse, Thomas",
        inbound="Wir unterstuetzen hier leider nicht.",
        sender="Thomas Kloeckner <thomas@lecon.eu>",
        user_name="Baher Al Hakim",
        expected_language="de",
    )
    assert any("inversion" in f.lower() for f in r.fabrications)


def test_signed_as_sender_is_inversion():
    r = verify_draft(
        "Liebe Nadine,\n\ndanke fuer die Nachricht.\n\nViele Gruesse,\nNadine",
        inbound="Kannst du mir den Zugang schicken?",
        sender="Nadine <nadine@medicus.ai>",
        user_name="Baher Al Hakim",
        expected_language="de",
    )
    assert any("inversion" in f.lower() for f in r.fabrications)


def test_normal_short_reply_is_not_inversion():
    # Greeting + one body line naming the recipient + "Thanks" must NOT trip
    # the signed-as-sender heuristic (the b286 false-positive class).
    r = verify_draft(
        "Hi Marcus,\n\nThanks for the time slots — Thursday works. Looking forward!",
        inbound="Does Thursday work? Best, Marcus",
        sender="Marcus <marcus@msd.com>",
        user_name="Baher Al Hakim",
    )
    assert all("inversion" not in f.lower() for f in r.fabrications)


# --- b286: German status claims + invented deadlines ---------------------


def test_german_completion_claim_flagged():
    r = verify_draft(
        "Kurt, danke — beide Konten sind nun abgedeckt und innerhalb der Limits.",
        inbound="Bitte decken Sie die Konten mit ausreichenden Mitteln.",
        expected_language="de",
    )
    assert any("abgedeckt" in c.lower() for c in r.status_claims)


def test_german_completion_claim_grounded_not_flagged():
    # The sender said it was transferred — acknowledging it is fine.
    r = verify_draft(
        "Danke — überwiesen ist angekommen, ich schließe den Fall.",
        inbound="Der Betrag wurde bereits überwiesen.",
        expected_language="de",
    )
    assert all("überwiesen" not in c.lower() for c in r.status_claims)


def test_invented_eod_deadline_flagged():
    r = verify_draft(
        "Thanks — I'll send the numbers and demo link by EOD.",
        inbound="Could you share the numbers whenever you have a look?",
    )
    assert any("deadline" in f.lower() for f in r.fabrications)


def test_grounded_deadline_not_flagged():
    r = verify_draft(
        "Sure — I'll get it to you by Friday.",
        inbound="Can you send it to me by Friday please?",
    )
    assert all("deadline" not in f.lower() for f in r.fabrications)
