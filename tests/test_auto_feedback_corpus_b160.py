"""b160: SFT export corpus integrity — down-weight self-labeled rows (don't
amplify them), a --human-rated-only curated export, and a per-sender cap."""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path

from scripts.export_feedback_jsonl import MAX_EXPORT_PAIRS, export


def _make_db(rows: list[dict], reply_pairs: list[dict] | None = None) -> Path:
    """rows: feedback_pairs dicts. reply_pairs: optional source rows (for authors)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(f.name)
    f.close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT, edited_reply TEXT, generated_draft TEXT DEFAULT '',
            rating INTEGER, edit_distance_pct REAL DEFAULT 0.5,
            created_at TEXT DEFAULT '2026-05-30', used_in_finetune INTEGER DEFAULT 0,
            feedback_note TEXT DEFAULT '', organic INTEGER DEFAULT 0, reply_pair_id INTEGER
        )
        """
    )
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, edited_reply, rating, edit_distance_pct, created_at, "
            "feedback_note, organic, reply_pair_id) VALUES (?,?,?,?,?,?,?,?)",
            (r["inbound_text"], r["edited_reply"], r["rating"], r.get("edit_distance_pct", 0.5),
             r.get("created_at", "2026-05-30"), r.get("feedback_note", ""), r.get("organic", 0),
             r.get("reply_pair_id")),
        )
    for rp in reply_pairs or []:
        conn.execute("INSERT INTO reply_pairs (id, inbound_author) VALUES (?, ?)", (rp["id"], rp["author"]))
    conn.commit()
    conn.close()
    return db_path


def _args(db: Path, out: Path, **over):
    base = dict(db=str(db), output=str(out), all=True, since=None, min_rating=3, min_edit_pct=0.05,
                no_persona=True, configs_dir="configs", dpo=False, curriculum=False, no_dedup=True,
                persona=None, human_rated_only=False)
    base.update(over)
    return argparse.Namespace(**base)


def _records(out: Path) -> list[str]:
    txt = out.read_text() + (out.parent / "valid.jsonl").read_text()
    return txt.splitlines()


def test_human_rated_only_excludes_self_labeled(tmp_path):
    db = _make_db([
        {"inbound_text": "Can you confirm the launch date for the new release?",
         "edited_reply": "Yes, the launch is set for next Friday afternoon.", "rating": 4,
         "feedback_note": "human review-queue edit"},
        {"inbound_text": "What's the status of the integration work this week?",
         "edited_reply": "Auto-captured: it's on track, demo Wednesday.", "rating": 4,
         "feedback_note": "auto-captured from sent email"},
        {"inbound_text": "Could you send over the latest figures when you get a chance?",
         "edited_reply": "Organic: attached the figures just now, let me know.", "rating": 3,
         "organic": 1},
    ])
    out = tmp_path / "train.jsonl"
    export(_args(db, out, human_rated_only=True))
    text = "\n".join(_records(out))
    assert "the launch is set for next Friday" in text   # human row kept
    assert "Auto-captured" not in text                   # auto-captured excluded
    assert "Organic:" not in text                        # organic excluded
    db.unlink()


def test_self_labeled_is_not_oversampled(tmp_path):
    """A recent rating-4 row is normally oversampled 2x; a self-labeled (auto-
    captured) rating-4 row is down-weighted (quality capped at 3) so it is NOT."""
    recent = "2026-05-29"
    db = _make_db([
        {"inbound_text": "Human inbound asking about the quarterly budget review?",
         "edited_reply": "Human reply: the budget review is confirmed for Monday.", "rating": 4,
         "created_at": recent, "feedback_note": "human edit"},
        {"inbound_text": "Self-labeled inbound asking about the quarterly budget review?",
         "edited_reply": "Self reply: the budget review is confirmed for Monday.", "rating": 4,
         "created_at": recent, "feedback_note": "auto-captured from sent email"},
    ])
    out = tmp_path / "train.jsonl"
    export(_args(db, out))  # rows dated within the 90-day oversampling window
    records = _records(out)
    human = sum(1 for r in records if "Human reply" in r)
    self_labeled = sum(1 for r in records if "Self reply" in r)
    assert human == 2          # human rating-4 recent -> oversampled 2x
    assert self_labeled == 1   # self-labeled down-weighted (quality capped 3) -> not boosted
    db.unlink()


def test_per_sender_cap_limits_one_author(tmp_path):
    """b160: one chatty correspondent can't dominate — pairs beyond max_per_sender
    from a single linked author are dropped before the global cap."""
    cap = max(20, MAX_EXPORT_PAIRS // 20)
    n = cap + 15
    rows = []
    reply_pairs = []
    for i in range(n):
        rows.append({
            "inbound_text": f"Distinct question number {i} about topic {i} and the plan?",
            "edited_reply": f"Distinct reply number {i} with enough detail to qualify here.",
            "rating": 3, "organic": 1, "reply_pair_id": i + 1,
        })
        reply_pairs.append({"id": i + 1, "author": "chatty@example.com"})
    db = _make_db(rows, reply_pairs)
    out = tmp_path / "train.jsonl"
    export(_args(db, out))
    # unique inbound lines from that author in the output must not exceed the cap
    uniq = {line for line in _records(out) if "Distinct question number" in line}
    assert 0 < len(uniq) <= cap
    db.unlink()
