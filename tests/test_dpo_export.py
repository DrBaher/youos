"""Tests for DPO preference-pair export.

b275: a genuine DPO pair holds the inbound FIXED — chosen = the reply you sent,
rejected = the model's draft for that SAME message. (The old exporter paired a
high-rated reply with an unrelated low-rated reply matched by length, a nonsense
contrast across different prompts.)
"""

import argparse
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

from scripts.export_feedback_jsonl import export_dpo


def _make_db(rows: list[dict]) -> Path:
    """Create a temp DB with feedback_pairs. Each row: {inbound, draft, reply,
    ed, organic}."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(f.name)
    f.close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT, edited_reply TEXT, generated_draft TEXT,
            rating INTEGER, edit_distance_pct REAL, organic INTEGER DEFAULT 0,
            feedback_note TEXT DEFAULT ''
        )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, "
            "edit_distance_pct, organic) VALUES (?, ?, ?, ?, ?)",
            (r["inbound"], r["draft"], r["reply"], r.get("ed", 0.5), r.get("organic", 0)),
        )
    conn.commit()
    conn.close()
    return db_path


def _args(db: Path) -> argparse.Namespace:
    # no_persona keeps the export hermetic (no dependency on repo configs).
    return argparse.Namespace(db=str(db), dpo=True, no_persona=True, configs_dir=".")


def _run(args, tmp_path) -> Path:
    import scripts.export_feedback_jsonl as mod

    orig = mod.ROOT_DIR
    mod.ROOT_DIR = tmp_path
    try:
        export_dpo(args)
    finally:
        mod.ROOT_DIR = orig
    return tmp_path / "data" / "dpo_train.jsonl"


def test_dpo_pairs_same_inbound_draft_vs_reply(tmp_path):
    """chosen = your reply, rejected = the model's draft, for the SAME inbound."""
    db = _make_db([
        {
            "inbound": "Can you physically sign the invoice and send it back?",
            "draft": "Sure, I'll send it over tomorrow.",
            "reply": "Sure, but bear with me — working from home, so probably next week.",
            "ed": 0.6,
            "organic": 0,
        }
    ])
    out = _run(_args(db), tmp_path)
    assert out.exists()
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["prompt"].startswith("Can you physically sign")
    assert rec["chosen"].startswith("Sure, but bear with me")   # what you sent
    assert rec["rejected"].startswith("Sure, I'll send it over tomorrow")  # the over-commit draft
    assert "system" not in rec  # no_persona
    db.unlink()


def test_dpo_excludes_organic_backfill(tmp_path):
    """Organic rows (no real model draft) carry no preference signal."""
    db = _make_db([
        {"inbound": "Question about the report?", "draft": "a model draft long enough",
         "reply": "your real reply long enough", "ed": 0.5, "organic": 1},
    ])
    assert not _run(_args(db), tmp_path).exists()
    db.unlink()


def test_dpo_filters_contrast_band(tmp_path):
    """Near-identical (no signal) and near-orthogonal (noise) pairs are dropped."""
    db = _make_db([
        {"inbound": "A near-identical case here", "draft": "Thanks, I'll do that.",
         "reply": "Thanks, I will do that.", "ed": 0.05, "organic": 0},   # too low
        {"inbound": "An orthogonal case entirely", "draft": "completely unrelated draft text",
         "reply": "totally different reply about other things", "ed": 0.97, "organic": 0},  # too high
    ])
    assert not _run(_args(db), tmp_path).exists()
    db.unlink()


def test_dpo_skips_identical_draft_and_reply(tmp_path):
    """If the model nailed it (draft == reply), there's nothing to prefer."""
    db = _make_db([
        {"inbound": "Quick question here?", "draft": "Same exact text reply here",
         "reply": "Same exact text reply here", "ed": 0.0, "organic": 0},
    ])
    assert not _run(_args(db), tmp_path).exists()
    db.unlink()


def test_dpo_output_is_owner_only(tmp_path):
    """b151: exported JSONL holds raw email bodies/drafts — not world-readable."""
    db = _make_db([
        {"inbound": "Can we meet to discuss the timeline?", "draft": "Sure, sending times tomorrow.",
         "reply": "Sure, but next week works better for me.", "ed": 0.5, "organic": 0},
    ])
    out = _run(_args(db), tmp_path)
    assert out.exists()
    assert oct(stat.S_IMODE(os.stat(out).st_mode)) == "0o600"
    db.unlink()
