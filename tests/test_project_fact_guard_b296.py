"""b296: project facts keyed on a stopword ("and", from the greedy
"project <word>" extractor) substring-matched almost every inbound and injected
garbage ("tomorrow's full review meeting") into every draft. Guard both the
write (extractor) and read (injection) sides."""
from __future__ import annotations

import sqlite3

from app.core.facts_extractor import _extract_project_key
from app.generation.service import _project_key_matches, lookup_facts


def test_project_key_matches_rejects_stopwords_and_requires_whole_word():
    assert not _project_key_matches("and", "meet tomorrow and then sync")
    assert not _project_key_matches("the", "the meeting is set")
    assert not _project_key_matches("fl", "fl is too short")            # <3 chars
    assert _project_key_matches("samba", "let's sync on project samba")  # real key
    assert _project_key_matches("flo", "flo health partnership")         # 3 chars, whole word
    assert not _project_key_matches("samba", "sambaesque melody")        # substring, not word


def test_extract_project_key_rejects_stopword_capture():
    # "project and have capacity" must NOT become a project keyed "and".
    assert _extract_project_key("we discussed project and have capacity", None) == "default"
    assert _extract_project_key("aligned on project successfully last week", None) == "default"
    assert _extract_project_key("the Samba project timeline slipped", None) == "samba"
    assert _extract_project_key("nothing relevant mentioned here", None) == "default"  # no trigger
    assert _extract_project_key("anything", "PreNUDGE") == "prenudge"   # explicit name honored


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE memory (id INTEGER PRIMARY KEY, type TEXT, key TEXT, fact TEXT, "
        "tags TEXT, created_at TEXT, updated_at TEXT, confidence REAL)"
    )
    return conn


def test_lookup_facts_drops_stopword_keyed_project_fact():
    conn = _memory_db()
    conn.execute("INSERT INTO memory(type,key,fact,tags) VALUES "
                 "('project','and','Stakeholder: in tomorrow''s full review meeting','[]')")
    conn.execute("INSERT INTO memory(type,key,fact,tags) VALUES "
                 "('project','samba','Kickoff is 20 July','[]')")
    conn.commit()
    facts = lookup_facts(
        sender=None,
        inbound_text="Quick update on project Samba and the timeline.",
        database_url="",
        conn=conn,
    )
    keys = {f["key"] for f in facts}
    assert "samba" in keys                 # meaningful key, whole-word match → injected
    assert "and" not in keys               # stopword key present in inbound → NOT injected
    assert all("full review meeting" not in f["fact"] for f in facts)
