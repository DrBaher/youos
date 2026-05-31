"""b167: draft-quality outcome join (draft_events -> feedback_pairs).

Regression coverage for the structurally-disconnected ground-truth signal.
``summarize_draft_events`` used to join ``draft_events`` -> ``draft_history`` on
inbound+draft text, which matched 0 rows on a live instance (draft_history is
near-empty and its ``edit_distance_pct`` is never populated), so the outcome
dicts were always ``{}`` and the autoresearch draft-quality weighting was inert.

The real edit-distance ground truth lives in ``feedback_pairs.edit_distance_pct``
and the only stable shared key is ``inbound_text``. These tests assert the fixed
join produces real coverage + per-cohort averages, that a zero-overlap case is
reported honestly, and that the downstream scorer turns that outcome dict into
non-uniform case weights (i.e. the optimizer weighting is no longer inert).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.autoresearch.scorer import draft_quality_case_weights
from app.core.stats import summarize_draft_events
from app.db.bootstrap import resolve_sqlite_path

# --- exact live schemas (captured read-only from the baheros instance) ------

_DDL_DRAFT_EVENTS = """
CREATE TABLE draft_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL,
    account_email TEXT, sender TEXT, sender_type TEXT,
    detected_mode TEXT, intent TEXT, confidence TEXT,
    confidence_reason TEXT, model_used TEXT, retrieval_method TEXT,
    exemplar_ids TEXT NOT NULL DEFAULT '[]', length_flag TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_DRAFT_HISTORY = """
CREATE TABLE draft_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL,
    sender TEXT,
    generated_draft TEXT NOT NULL,
    final_reply TEXT,
    edit_distance_pct REAL,
    confidence TEXT,
    model_used TEXT,
    retrieval_method TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_FEEDBACK_PAIRS = """
CREATE TABLE feedback_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL,
    generated_draft TEXT NOT NULL,
    edited_reply TEXT NOT NULL,
    feedback_note TEXT,
    rating INTEGER,
    used_in_finetune INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    edit_distance_pct REAL,
    reply_pair_id INTEGER,
    organic BOOLEAN DEFAULT 0,
    edit_categories TEXT,
    precedents_used TEXT
)
"""


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(_DDL_DRAFT_EVENTS)
    conn.execute(_DDL_DRAFT_HISTORY)
    conn.execute(_DDL_FEEDBACK_PAIRS)
    conn.commit()
    return conn


def _add_draft_event(conn, inbound, draft, *, sender_type, confidence, length_flag=None):
    conn.execute(
        """INSERT INTO draft_events
               (inbound_text, generated_draft, sender_type, confidence, length_flag)
           VALUES (?, ?, ?, ?, ?)""",
        (inbound, draft, sender_type, confidence, length_flag),
    )


def _add_feedback(conn, inbound, *, edit_distance_pct, draft="real reply", edited="e", organic=1):
    # Draft text intentionally DIFFERS from the draft_events draft: on the live
    # DB feedback_pairs.generated_draft is the organic/full reply, not the logged
    # draft, which is exactly why an inbound+draft join matches ~nothing.
    conn.execute(
        """INSERT INTO feedback_pairs
               (inbound_text, generated_draft, edited_reply, edit_distance_pct, organic)
           VALUES (?, ?, ?, ?, ?)""",
        (inbound, draft, edited, edit_distance_pct, organic),
    )


@pytest.fixture()
def db_url(tmp_path):
    """A temp-DB ``database_url`` whose resolved path round-trips to our file.

    Picks the URL form that ``resolve_sqlite_path`` maps back to our seeded file,
    so the test exercises the real production resolution path.
    """
    db_path = tmp_path / "youos.db"
    candidates = [f"sqlite:///{db_path}", str(db_path), db_path.as_uri()]
    for url in candidates:
        try:
            if resolve_sqlite_path(url) == db_path:
                return url, db_path
        except Exception:  # noqa: BLE001 - probing which URL form resolves
            continue
    raise AssertionError(f"no database_url form resolved to {db_path}; tried {candidates}")


def test_outcome_join_matches_and_aggregates(db_url):
    """Realistic case: draft_events JOIN feedback_pairs ON inbound_text yields
    real coverage and non-empty per-cohort averages — driven by feedback_pairs,
    NOT draft_history (which stays empty here, as on the live DB)."""
    url, db_path = db_url
    conn = _make_db(db_path)
    try:
        # Two cohorts with distinct edit-distance profiles:
        # external_client/high barely edited; personal/low heavily edited.
        _add_draft_event(conn, "inbA", "stub-signature", sender_type="external_client", confidence="high")
        _add_draft_event(conn, "inbB", "stub-signature", sender_type="personal", confidence="low")
        # Fan-out: inbA has two outcome rows (mean 0.10), inbB has one (0.60).
        _add_feedback(conn, "inbA", edit_distance_pct=0.05)
        _add_feedback(conn, "inbA", edit_distance_pct=0.15)
        _add_feedback(conn, "inbB", edit_distance_pct=0.60)
        conn.commit()
    finally:
        conn.close()

    summary = summarize_draft_events(url)
    outcome = summary["outcome"]

    # Coverage counts distinct draft_events that found an outcome (fan-out
    # collapsed first, so n is by draft_event, not by feedback row).
    assert outcome["matched"] == 2

    by_sender = outcome["avg_edit_distance_by_sender_type"]
    assert set(by_sender) == {"external_client", "personal"}
    assert by_sender["external_client"]["n"] == 1
    # inbA's two feedback rows are averaged first -> 0.10, not double-counted.
    assert by_sender["external_client"]["avg_edit_distance"] == pytest.approx(0.10, abs=1e-6)
    assert by_sender["personal"]["avg_edit_distance"] == pytest.approx(0.60, abs=1e-6)

    by_conf = outcome["avg_edit_distance_by_confidence"]
    assert set(by_conf) == {"high", "low"}
    assert by_conf["high"]["avg_edit_distance"] == pytest.approx(0.10, abs=1e-6)
    assert by_conf["low"]["avg_edit_distance"] == pytest.approx(0.60, abs=1e-6)


def test_zero_overlap_reports_matched_zero_honestly(db_url):
    """No inbound_text overlap (and empty draft_history) -> matched is an honest
    0 and the cohort dicts are empty. The failure is VISIBLE, not swallowed."""
    url, db_path = db_url
    conn = _make_db(db_path)
    try:
        _add_draft_event(conn, "inbX", "stub", sender_type="personal", confidence="low")
        _add_feedback(conn, "DIFFERENT_INBOUND", edit_distance_pct=0.42)
        conn.commit()
    finally:
        conn.close()

    summary = summarize_draft_events(url)
    outcome = summary["outcome"]
    assert summary["total"] == 1  # drafts are still counted
    assert outcome["matched"] == 0
    assert outcome["avg_edit_distance_by_sender_type"] == {}
    assert outcome["avg_edit_distance_by_confidence"] == {}


def test_feedback_without_edit_distance_is_ignored(db_url):
    """A feedback row with NULL edit_distance_pct must not create a phantom
    match — keeps ``matched`` an honest signal-bearing counter."""
    url, db_path = db_url
    conn = _make_db(db_path)
    try:
        _add_draft_event(conn, "inbN", "stub", sender_type="personal", confidence="low")
        _add_feedback(conn, "inbN", edit_distance_pct=None)
        conn.commit()
    finally:
        conn.close()

    outcome = summarize_draft_events(url)["outcome"]
    assert outcome["matched"] == 0
    assert outcome["avg_edit_distance_by_sender_type"] == {}


def test_inbound_plus_draft_join_would_have_missed(db_url):
    """Documents the original bug: matching on inbound AND draft text (the old
    behavior) finds nothing here because the draft texts differ between tables,
    yet the new inbound-only join still lands. Guards against a regression to
    the over-strict join."""
    url, db_path = db_url
    conn = _make_db(db_path)
    try:
        _add_draft_event(conn, "inbZ", "DRAFT_TEXT_A", sender_type="internal", confidence="high")
        _add_feedback(conn, "inbZ", edit_distance_pct=0.30, draft="ORGANIC_REPLY_B")
        conn.commit()
    finally:
        conn.close()

    outcome = summarize_draft_events(url)["outcome"]
    assert outcome["matched"] == 1, "inbound-only join must still match despite differing draft text"
    assert outcome["avg_edit_distance_by_sender_type"]["internal"]["avg_edit_distance"] == pytest.approx(0.30, abs=1e-6)


def test_downstream_scorer_produces_real_weights(db_url):
    """The optimizer's draft-quality weighting is no longer inert: the outcome
    dict produced by summarize_draft_events drives non-uniform case weights."""
    url, db_path = db_url
    conn = _make_db(db_path)
    try:
        _add_draft_event(conn, "inb1", "stub", sender_type="external_client", confidence="high")
        _add_draft_event(conn, "inb2", "stub", sender_type="personal", confidence="low")
        _add_feedback(conn, "inb1", edit_distance_pct=0.05)
        _add_feedback(conn, "inb2", edit_distance_pct=0.70)
        conn.commit()
    finally:
        conn.close()

    summary = summarize_draft_events(url)
    outcome = summary["outcome"]

    # Precondition: the join produced the per-cohort signal the scorer consumes.
    # Without the b167 fix this dict is {} and weighting is inert.
    assert outcome["avg_edit_distance_by_sender_type"], (
        "summarize_draft_events must yield per-sender outcome data for weighting"
    )

    # draft_quality_case_weights consumes the FULL summary (reads summary["outcome"]).
    weights = draft_quality_case_weights(summary)

    # Real weights came out, and the heavily-edited cohort is weighted higher
    # than the barely-edited one (the whole point of the objective).
    assert weights, "expected non-empty case weights from real outcome data"
    assert set(weights) >= {"personal", "external_client"}
    assert weights["personal"] > weights["external_client"]

    # And an empty outcome still collapses to {} (uniform weighting, no-op),
    # proving the weights above are driven by the join, not a constant default.
    assert draft_quality_case_weights(
        {"outcome": {"matched": 0, "avg_edit_distance_by_sender_type": {}, "avg_edit_distance_by_confidence": {}}}
    ) == {}
