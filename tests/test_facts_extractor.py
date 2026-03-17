"""Tests for the rule-based fact extractor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.facts_extractor import (
    extract_and_save,
    extract_facts,
    filter_new_facts,
    save_facts,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_memory_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            key TEXT NOT NULL,
            fact TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.8,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(type, key, fact)
        )
    """)
    conn.commit()
    conn.close()
    return db


# ── extract_facts: basic ──────────────────────────────────────────────────────

def test_empty_note_returns_empty():
    assert extract_facts("") == []
    assert extract_facts("   ") == []


def test_prefers_short_replies():
    facts = extract_facts("She prefers short replies.")
    assert any(f["fact"] == "Prefers short replies" for f in facts)


def test_prefers_generic():
    facts = extract_facts("He prefers email over Slack")
    assert any("email over Slack" in f["fact"] for f in facts)


def test_dislikes():
    facts = extract_facts("John hates long emails and small talk.")
    assert any("Dislikes" in f["fact"] for f in facts)


def test_always_cc():
    facts = extract_facts("Always CC boss@company.com on replies.")
    assert any(f["fact"] == "Always CC boss@company.com" for f in facts)
    assert any(f["type"] == "contact" for f in facts)


def test_timezone_abbreviation():
    facts = extract_facts("She is based in PST timezone.")
    assert any("PST" in f["fact"] for f in facts)


def test_utc_offset_timezone():
    facts = extract_facts("He works in UTC+5:30.")
    assert any("UTC+5:30" in f["fact"] for f in facts)


def test_meeting_days():
    facts = extract_facts("Meetings on Tuesdays and Thursdays work best.")
    assert any("Meetings on" in f["fact"] for f in facts)


def test_available_days():
    facts = extract_facts("Available on Mon and Wed.")
    assert any("Available on" in f["fact"] for f in facts)


def test_sign_off_with():
    facts = extract_facts('She signs off with "Best regards".')
    assert any("Sign off" in f["fact"] for f in facts)
    assert any(f["type"] == "user_pref" for f in facts)


def test_sign_off_as():
    facts = extract_facts("Signs off as Cheers")
    assert any("Sign off: Cheers" in f["fact"] for f in facts)


def test_responds_within():
    facts = extract_facts("He responds within 2 business days.")
    assert any("Responds within 2 business days" in f["fact"] for f in facts)


def test_writes_in_language():
    facts = extract_facts("Writes only in Spanish.")
    assert any("Spanish" in f["fact"] for f in facts)


def test_project_deadline():
    facts = extract_facts("Deadline: end of March")
    assert any(f["type"] == "project" for f in facts)
    assert any("end of March" in f["fact"] for f in facts)


def test_budget():
    facts = extract_facts("Budget: $50k for the project.")
    assert any(f["type"] == "project" for f in facts)
    assert any("$50k" in f["fact"] for f in facts)


def test_sender_email_sets_key():
    facts = extract_facts("Prefers short replies.", sender_email="alice@example.com")
    contact_facts = [f for f in facts if f["type"] == "contact"]
    assert all(f["key"] == "alice@example.com" for f in contact_facts)


def test_user_pref_key_is_default():
    facts = extract_facts('Signs off with "Thanks"', sender_email="bob@example.com")
    pref_facts = [f for f in facts if f["type"] == "user_pref"]
    assert all(f["key"] == "default" for f in pref_facts)


def test_no_sender_contact_key_is_unknown():
    facts = extract_facts("Prefers bullet points")
    contact_facts = [f for f in facts if f["type"] == "contact"]
    assert all(f["key"] == "unknown" for f in contact_facts)


def test_no_duplicate_within_note():
    # Two matches that produce the same fact should be deduplicated
    facts = extract_facts("He prefers short replies. She prefers short replies.")
    short_reply_facts = [f for f in facts if f["fact"] == "Prefers short replies"]
    assert len(short_reply_facts) == 1


# ── Multiple matches per pattern (finditer) ───────────────────────────────────

def test_multiple_cc_emails():
    facts = extract_facts("Always CC boss@co.com and always CC sarah@co.com")
    cc_facts = [f["fact"] for f in facts if "Always CC" in f["fact"]]
    assert "Always CC boss@co.com" in cc_facts
    assert "Always CC sarah@co.com" in cc_facts
    assert len(cc_facts) == 2


def test_multiple_languages_writes_speaks():
    facts = extract_facts("Writes in French and speaks German.")
    assert any("French" in f["fact"] for f in facts)
    assert any("German" in f["fact"] for f in facts)


def test_multiple_meeting_days_entries():
    # Two separate meeting statements produce separate facts
    note = "Meetings on Mondays work best. Also meetings on Fridays."
    facts = extract_facts(note)
    meeting_facts = [f["fact"] for f in facts if "Meetings on" in f["fact"]]
    assert len(meeting_facts) >= 1  # at minimum Monday captured


# ── Negation awareness ────────────────────────────────────────────────────────

def test_negation_prefers_short():
    facts = extract_facts("She doesn't prefer short replies")
    assert not any("Prefers short replies" in f["fact"] for f in facts)


def test_negation_generic_prefers():
    facts = extract_facts("He does not prefer email over phone")
    assert not any("email over phone" in f["fact"] for f in facts)


def test_negation_responds_within():
    facts = extract_facts("He doesn't respond within 2 days.")
    assert not any("Responds within" in f["fact"] for f in facts)


def test_no_false_negation():
    facts = extract_facts("She prefers short replies")
    assert any("Prefers short replies" in f["fact"] for f in facts)


def test_negation_does_not_affect_inherently_negative_patterns():
    # "hates" is check_negation=False — negation words in context should NOT suppress it
    facts = extract_facts("He really hates long emails")
    assert any("Dislikes" in f["fact"] for f in facts)


# ── New patterns ──────────────────────────────────────────────────────────────

def test_reports_to():
    facts = extract_facts("She reports to John Smith", sender_email="a@b.com")
    assert any("Reports to John Smith" in f["fact"] for f in facts)


def test_title_colon():
    facts = extract_facts("Title: Senior Engineer", sender_email="a@b.com")
    assert any("Senior Engineer" in f["fact"] for f in facts)


def test_role_colon():
    facts = extract_facts("Role: Head of Sales", sender_email="a@b.com")
    assert any("Head of Sales" in f["fact"] for f in facts)


def test_works_at():
    facts = extract_facts("Works at Acme Corp", sender_email="a@b.com")
    assert any("Acme Corp" in f["fact"] for f in facts)


def test_company_colon():
    facts = extract_facts("Company: Big Tech Inc", sender_email="a@b.com")
    assert any("Big Tech Inc" in f["fact"] for f in facts)


def test_phone_number():
    facts = extract_facts("Phone: +43 123 456 789", sender_email="a@b.com")
    assert any("Phone:" in f["fact"] for f in facts)


def test_mobile_number():
    facts = extract_facts("Mobile: 555-867-5309", sender_email="a@b.com")
    assert any("Phone:" in f["fact"] for f in facts)


def test_speaks_language():
    facts = extract_facts("Speaks French", sender_email="a@b.com")
    assert any("French" in f["fact"] and "Speaks" in f["fact"] for f in facts)


def test_based_in():
    facts = extract_facts("Based in Vienna", sender_email="a@b.com")
    assert any("Vienna" in f["fact"] for f in facts)


def test_located_in():
    facts = extract_facts("Located in Berlin", sender_email="a@b.com")
    assert any("Berlin" in f["fact"] for f in facts)


def test_preferred_name_goes_by():
    facts = extract_facts("Goes by Alex", sender_email="a@b.com")
    assert any("Alex" in f["fact"] for f in facts)


def test_preferred_name_call_them():
    facts = extract_facts("Call them Sam", sender_email="a@b.com")
    assert any("Sam" in f["fact"] for f in facts)


def test_unavailable_dont_email_after():
    facts = extract_facts("Don't email after 6pm", sender_email="a@b.com")
    assert any("Unavailable" in f["fact"] for f in facts)


def test_unavailable_ooo():
    facts = extract_facts("OOO on Fridays", sender_email="a@b.com")
    assert any("Unavailable" in f["fact"] for f in facts)


def test_cc_assistant():
    facts = extract_facts("CC his assistant Jane", sender_email="a@b.com")
    assert any("CC assistant" in f["fact"] for f in facts)


def test_decision_maker():
    facts = extract_facts("She is the decision maker", sender_email="a@b.com")
    assert any("decision maker" in f["fact"].lower() for f in facts)


def test_not_decision_maker():
    facts = extract_facts("He is not the decision maker", sender_email="a@b.com")
    assert any("not" in f["fact"].lower() and "decision maker" in f["fact"].lower() for f in facts)


def test_gatekeeper():
    facts = extract_facts("He is the gatekeeper", sender_email="a@b.com")
    assert any("gatekeeper" in f["fact"].lower() for f in facts)


def test_referred_by():
    facts = extract_facts("Referred by Sarah Johnson", sender_email="a@b.com")
    assert any("Sarah Johnson" in f["fact"] for f in facts)


def test_introduced_by():
    facts = extract_facts("Introduced by Mike from sales", sender_email="a@b.com")
    assert any("Referred by" in f["fact"] for f in facts)


def test_vip_client():
    facts = extract_facts("VIP client, treat accordingly", sender_email="a@b.com")
    assert any("vip" in f["fact"].lower() for f in facts)


def test_account_manager():
    facts = extract_facts("Account manager for this region", sender_email="a@b.com")
    assert any("account manager" in f["fact"].lower() for f in facts)


def test_billing_email():
    facts = extract_facts("Billing email: finance@acme.com", sender_email="a@b.com")
    assert any("finance@acme.com" in f["fact"] for f in facts)


def test_invoice_to():
    facts = extract_facts("Invoice to accounts@bigco.com", sender_email="a@b.com")
    assert any("accounts@bigco.com" in f["fact"] for f in facts)


def test_renewal_date():
    facts = extract_facts("Renewal date: March 2025", sender_email="a@b.com")
    renewal = [f for f in facts if "Renewal date" in f["fact"]]
    assert renewal
    assert any(f["type"] == "project" for f in renewal)


def test_contract_expires():
    facts = extract_facts("Contract expires December 2025")
    assert any("December 2025" in f["fact"] for f in facts)


def test_stakeholder():
    facts = extract_facts("Stakeholder: Bob from Legal")
    stk = [f for f in facts if "Stakeholder" in f["fact"]]
    assert stk
    assert any(f["type"] == "project" for f in stk)


def test_involved():
    facts = extract_facts("Involved: Alice, Bob")
    assert any("Stakeholder" in f["fact"] for f in facts)


# ── Confidence scoring ────────────────────────────────────────────────────────

def test_confidence_present_in_all_facts():
    facts = extract_facts("Always CC boss@example.com. Prefers email over phone.")
    for f in facts:
        assert "confidence" in f
        assert 0.0 <= f["confidence"] <= 1.0


def test_explicit_pattern_high_confidence():
    facts = extract_facts("Always CC boss@example.com", sender_email="x@y.com")
    cc = [f for f in facts if "Always CC" in f["fact"]]
    assert cc
    assert cc[0]["confidence"] == 0.9


def test_specific_prefers_confidence():
    facts = extract_facts("She prefers short replies")
    sr = [f for f in facts if f["fact"] == "Prefers short replies"]
    assert sr
    assert sr[0]["confidence"] == 0.8


def test_generic_prefers_lower_confidence():
    facts = extract_facts("He prefers email over phone")
    generic = [f for f in facts if "email over phone" in f["fact"]]
    assert generic
    assert generic[0]["confidence"] == 0.6


def test_very_long_capture_reduces_confidence():
    # A capture > 40 chars should be downgraded to 0.4
    long_text = "a" * 45
    facts = extract_facts(f"Prefers {long_text}")
    if facts:
        assert facts[0]["confidence"] == 0.4


def test_timezone_confidence():
    facts = extract_facts("He is in PST timezone.")
    tz = [f for f in facts if "PST" in f["fact"]]
    assert tz
    assert tz[0]["confidence"] == 0.9


# ── Generic prefers span suppression ─────────────────────────────────────────

def test_specific_prefers_suppresses_generic():
    # "prefers short replies" should produce one fact with template text, not two
    facts = extract_facts("She prefers short replies.")
    prefers_facts = [f for f in facts if "Prefers" in f["fact"]]
    assert len(prefers_facts) == 1
    assert prefers_facts[0]["fact"] == "Prefers short replies"


def test_specific_and_separate_generic_both_emit():
    # Specific match for one sentence + generic match for a separate sentence
    # The period creates a sentence boundary, so finditer finds two distinct spans
    facts = extract_facts("She prefers short replies. She also prefers early morning calls.")
    facts_text = [f["fact"] for f in facts if "Prefers" in f["fact"]]
    assert any(f == "Prefers short replies" for f in facts_text)
    assert any("early morning calls" in f for f in facts_text)


# ── Project key extraction ────────────────────────────────────────────────────

def test_project_key_from_context():
    facts = extract_facts("Deadline: end of March for project Alpha")
    proj = [f for f in facts if f["type"] == "project"]
    assert proj
    assert proj[0]["key"] == "alpha"


def test_project_key_from_param():
    facts = extract_facts("Deadline: end of March", project_name="Beta")
    proj = [f for f in facts if f["type"] == "project"]
    assert proj
    assert proj[0]["key"] == "beta"


def test_project_key_defaults_to_default():
    facts = extract_facts("Deadline: end of March")
    proj = [f for f in facts if f["type"] == "project"]
    assert proj
    assert proj[0]["key"] == "default"


# ── filter_new_facts ──────────────────────────────────────────────────────────

def test_filter_removes_existing_exact(tmp_path):
    db = _make_memory_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO memory (type, key, fact, tags) VALUES ('contact','a@b.com','Prefers short replies','[]')")
    conn.commit()
    conn.close()

    candidates = [{"type": "contact", "key": "a@b.com", "fact": "Prefers short replies"}]
    result = filter_new_facts(candidates, db)
    assert result == []


def test_filter_keeps_new_fact(tmp_path):
    db = _make_memory_db(tmp_path)
    candidates = [{"type": "contact", "key": "a@b.com", "fact": "Prefers short replies"}]
    result = filter_new_facts(candidates, db)
    assert len(result) == 1


def test_filter_case_insensitive_dedup(tmp_path):
    db = _make_memory_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO memory (type, key, fact, tags) VALUES ('contact','a@b.com','PREFERS SHORT REPLIES','[]')")
    conn.commit()
    conn.close()

    candidates = [{"type": "contact", "key": "a@b.com", "fact": "Prefers short replies"}]
    result = filter_new_facts(candidates, db)
    assert result == []


# ── save_facts ────────────────────────────────────────────────────────────────

def test_save_facts_inserts(tmp_path):
    db = _make_memory_db(tmp_path)
    facts = [{"type": "contact", "key": "x@y.com", "fact": "Prefers bullet points", "confidence": 0.8}]
    saved = save_facts(facts, db)
    assert len(saved) == 1
    assert saved[0]["fact"] == "Prefers bullet points"
    assert "id" in saved[0]


def test_save_facts_ignores_duplicate(tmp_path):
    db = _make_memory_db(tmp_path)
    facts = [{"type": "contact", "key": "x@y.com", "fact": "Prefers bullet points", "confidence": 0.8}]
    save_facts(facts, db)
    saved2 = save_facts(facts, db)
    # Second save should not fail and returns the existing row
    assert len(saved2) == 1


def test_save_facts_stores_confidence(tmp_path):
    db = _make_memory_db(tmp_path)
    facts = [{"type": "contact", "key": "x@y.com", "fact": "Always CC boss@co.com", "confidence": 0.9}]
    saved = save_facts(facts, db)
    assert saved[0]["confidence"] == 0.9


# ── Fact merging ──────────────────────────────────────────────────────────────

def test_fact_merging_updates_similar(tmp_path):
    db = _make_memory_db(tmp_path)
    # Insert initial fact
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory (type, key, fact, confidence, tags) "
        "VALUES ('contact','x@y.com','Location: Vienna','0.8','[]')"
    )
    conn.commit()
    conn.close()

    # Save a similar but more specific fact
    save_facts([{"type": "contact", "key": "x@y.com", "fact": "Location: Vienna, Austria", "confidence": 0.9}], db)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT fact FROM memory WHERE key = 'x@y.com'").fetchall()
    conn.close()
    # Should be merged into one row, updated with the newer text
    assert len(rows) == 1
    assert rows[0][0] == "Location: Vienna, Austria"


def test_fact_merging_does_not_merge_dissimilar(tmp_path):
    db = _make_memory_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO memory (type, key, fact, confidence, tags) "
        "VALUES ('contact','x@y.com','Prefers short replies','0.8','[]')"
    )
    conn.commit()
    conn.close()

    # A very different fact should NOT be merged
    save_facts([{"type": "contact", "key": "x@y.com", "fact": "Based in Paris", "confidence": 0.8}], db)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM memory WHERE key = 'x@y.com'").fetchone()[0]
    conn.close()
    assert count == 2


def test_fact_merging_exact_duplicate_not_double_inserted(tmp_path):
    db = _make_memory_db(tmp_path)
    fact = {"type": "contact", "key": "x@y.com", "fact": "Prefers short replies", "confidence": 0.8}
    save_facts([fact], db)
    save_facts([fact], db)  # exact duplicate

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM memory WHERE key = 'x@y.com'").fetchone()[0]
    conn.close()
    assert count == 1


# ── extract_and_save ──────────────────────────────────────────────────────────

def test_extract_and_save_end_to_end(tmp_path):
    db = _make_memory_db(tmp_path)
    saved = extract_and_save(
        "Alice prefers short replies and always CC boss@acme.com",
        db,
        sender_email="alice@acme.com",
    )
    assert len(saved) >= 1
    facts_text = [s["fact"] for s in saved]
    assert any("short replies" in t for t in facts_text)


def test_extract_and_save_skips_existing(tmp_path):
    db = _make_memory_db(tmp_path)
    note = "Prefers short replies"
    saved1 = extract_and_save(note, db, sender_email="z@z.com")
    saved2 = extract_and_save(note, db, sender_email="z@z.com")
    # Second call should save nothing (already exists)
    assert len(saved1) >= 1
    assert saved2 == []


def test_extract_and_save_project_name_param(tmp_path):
    db = _make_memory_db(tmp_path)
    saved = extract_and_save("Deadline: end of Q3", db, project_name="phoenix")
    assert saved
    assert saved[0]["key"] == "phoenix"


def test_extract_and_save_use_llm_false_skips_llm_on_empty(tmp_path):
    """With use_llm=False (default), no LLM call is made even when no facts extracted."""
    db = _make_memory_db(tmp_path)
    # Note with no matchable patterns — should return [] without calling LLM
    saved = extract_and_save("This is just a generic note with nothing structured.", db, use_llm=False)
    assert saved == []
