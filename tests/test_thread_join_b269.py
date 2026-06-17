"""b269: draft<->sent join on the inbound message_id (turn-precise).

The old extraction matched a logged draft to the user's actual sent reply by
exact ``inbound_text`` equality (~1% hit on baheros), so genuine draft-vs-sent
pairs were lost to organic backfill and the loop fell back to regenerating a
draft per pair via the local LLM (~88% of the nightly). thread_id alone is too
coarse — a thread holds many turns and the agent drafted one. The coherent key is
the inbound ``message_id``: ``agent_pending_drafts.message_id`` (what the agent
drafted for) matched against ``reply_pairs.metadata_json.inbound_message_ids``
(what the reply answered). These tests pin:

  (i)   ``_agent_draft_for_reply`` recovers the agent's ACTUAL stored draft for
        the exact inbound the reply answered, preferring an in-app amendment;
  (ii)  capture emits a REAL pair (organic=0, real edit distance) with no LLM;
  (iii) the backfill relabels a pair the old join had mislabeled organic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.diff import similarity_ratio
from scripts.extract_auto_feedback import (
    _agent_draft_for_reply,
    _agent_drafts_by_message_id,
    _capture_organic_pairs,
    _inbound_message_ids,
    _relabel_mislabeled_organic_pairs,
)


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT, reply_text TEXT, thread_id TEXT,
            metadata_json TEXT, created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            quality_score REAL DEFAULT 1.0,
            auto_feedback_processed INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT, generated_draft TEXT, edited_reply TEXT,
            feedback_note TEXT, edit_distance_pct REAL, rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0, reply_pair_id INTEGER,
            organic BOOLEAN DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE agent_pending_drafts (
            id INTEGER PRIMARY KEY, message_id TEXT, thread_id TEXT,
            draft TEXT, amended_draft TEXT, created_at TEXT
        )"""
    )
    conn.commit()
    return conn


def _md(*message_ids: str) -> str:
    return json.dumps({"inbound_message_ids": list(message_ids), "account_email": "me@x.com"})


# --- helpers ---------------------------------------------------------------


def test_inbound_message_ids_parsing():
    assert _inbound_message_ids(_md("a", "b")) == ["a", "b"]
    assert _inbound_message_ids(None) == []
    assert _inbound_message_ids("not json") == []
    assert _inbound_message_ids(json.dumps({"other": 1})) == []


def test_agent_draft_map_prefers_amendment_and_latest(tmp_path):
    conn = _make_db(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, draft, amended_draft, created_at) VALUES (?,?,?,?)",
        ("M1", "auto draft", "user edited this", "2026-06-10 09:00:00"),
    )
    # A later draft for the same inbound overwrites the earlier one.
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, draft, created_at) VALUES (?,?,?)",
        ("M2", "second-turn draft", "2026-06-11 09:00:00"),
    )
    conn.commit()
    m = _agent_drafts_by_message_id(conn)
    assert m["M1"] == "user edited this"  # amendment preferred over raw draft
    assert m["M2"] == "second-turn draft"
    assert _agent_draft_for_reply(_md("M2"), m) == "second-turn draft"
    assert _agent_draft_for_reply(_md("nope"), m) is None


# --- capture via message_id join (no LLM) ----------------------------------


def test_capture_emits_real_pair_from_message_id_join(tmp_path):
    conn = _make_db(tmp_path / "t.db")
    reply = "Thanks — Tuesday 3pm works, I'll send an invite."
    draft = "Sure, Tuesday at 3 is fine. Talk then."
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, metadata_json, auto_feedback_processed) "
        "VALUES (?,?,?,0)",
        ("Can we meet Tuesday?", reply, _md("MID1")),
    )
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, draft, created_at) VALUES (?,?,?)",
        ("MID1", draft, "2026-06-16 09:00:00"),
    )
    conn.commit()

    count = _capture_organic_pairs(conn, dry_run=False)
    conn.commit()
    assert count == 1
    row = conn.execute(
        "SELECT organic, generated_draft, edited_reply, edit_distance_pct FROM feedback_pairs"
    ).fetchone()
    assert row["organic"] == 0  # REAL pair, not organic
    assert row["generated_draft"] == draft  # the agent's actual stored draft
    assert row["edited_reply"] == reply
    expected = round(1.0 - similarity_ratio(draft, reply), 4)
    assert abs(row["edit_distance_pct"] - expected) < 1e-6


def test_capture_no_match_stays_organic(tmp_path):
    """A reply whose inbound the agent never drafted is organic backfill."""
    conn = _make_db(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, metadata_json, auto_feedback_processed) "
        "VALUES (?,?,?,0)",
        ("Some inbound", "A real reply that is long enough.", _md("UNSEEN")),
    )
    conn.commit()
    assert _capture_organic_pairs(conn, dry_run=False) == 1
    assert conn.execute("SELECT organic FROM feedback_pairs").fetchone()["organic"] == 1


# --- backfill relabel ------------------------------------------------------


def test_relabel_rescues_mislabeled_organic_pair(tmp_path):
    conn = _make_db(tmp_path / "t.db")
    reply = "Appreciate it — let's go with the second option and revisit in Q3."
    draft = "Thanks! Option two sounds good to me. We can look again later."
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, metadata_json, auto_feedback_processed) "
        "VALUES (1,?,?,?,1)",
        ("Which option do you prefer?", reply, _md("MR")),
    )
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, draft, created_at) VALUES (?,?,?)",
        ("MR", draft, "2026-06-16 09:00:00"),
    )
    # The OLD pipeline stored this as organic (reply copied into both, ed=0).
    conn.execute(
        "INSERT INTO feedback_pairs (reply_pair_id, inbound_text, generated_draft, edited_reply, "
        "feedback_note, edit_distance_pct, rating, used_in_finetune, organic) "
        "VALUES (1,?,?,?,'organic pair — no YouOS draft',0.0,3,1,1)",
        ("Which option do you prefer?", reply, reply),
    )
    conn.commit()

    relabeled = _relabel_mislabeled_organic_pairs(conn, dry_run=False)
    conn.commit()
    assert relabeled == 1
    row = conn.execute(
        "SELECT organic, generated_draft, edit_distance_pct, used_in_finetune "
        "FROM feedback_pairs WHERE reply_pair_id=1"
    ).fetchone()
    assert row["organic"] == 0  # rescued into the learning signal
    assert row["generated_draft"] == draft  # real draft, not the reply-copy
    assert row["edit_distance_pct"] > 0  # real, non-zero distance
    assert row["used_in_finetune"] == 0  # eligible for the next finetune

    # Idempotent: a second pass finds nothing new (it's organic=0 now).
    assert _relabel_mislabeled_organic_pairs(conn, dry_run=False) == 0


def test_relabel_leaves_verbatim_pairs_organic(tmp_path):
    """If the agent's draft was sent verbatim, it's a genuine organic/verbatim
    record, not a draft-vs-sent edit pair — must stay organic."""
    conn = _make_db(tmp_path / "t.db")
    text = "Sounds good, see you then."
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, metadata_json, auto_feedback_processed) "
        "VALUES (1,?,?,?,1)",
        ("ok?", text, _md("MV")),
    )
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, draft, created_at) VALUES (?,?,?)",
        ("MV", text, "2026-06-16 09:00:00"),
    )
    conn.execute(
        "INSERT INTO feedback_pairs (reply_pair_id, inbound_text, generated_draft, edited_reply, "
        "feedback_note, edit_distance_pct, rating, used_in_finetune, organic) "
        "VALUES (1,?,?,?,'organic',0.0,3,1,1)",
        ("ok?", text, text),
    )
    conn.commit()
    assert _relabel_mislabeled_organic_pairs(conn, dry_run=False) == 0


def test_relabel_noops_on_minimal_schema(tmp_path):
    """No metadata_json / no queue → clean no-op."""
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, reply_text TEXT)")
    conn.execute(
        "CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, reply_pair_id INTEGER, "
        "edited_reply TEXT, organic BOOLEAN DEFAULT 1)"
    )
    conn.commit()
    assert _relabel_mislabeled_organic_pairs(conn, dry_run=False) == 0
