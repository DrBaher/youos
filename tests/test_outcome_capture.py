"""Outcome capture (b224): pair queued YouOS drafts with the user's real sends."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.agent import outcome_capture, store


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def _msg(mid: str, frm: str, text: str) -> dict:
    return {
        "id": mid,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": frm}],
            "body": {"data": _b64(text)},
        },
    }


class _FakeSource:
    def __init__(self, threads):
        self.threads = threads

    def get_thread(self, *, account, thread_id):
        return self.threads[thread_id]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir()
    (tmp_path / "configs").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "schema.sql").write_text((Path(__file__).resolve().parents[1] / "docs" / "schema.sql").read_text())
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")
    from app.core.config import load_config
    load_config.cache_clear()
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database
    bootstrap_database()
    return f"sqlite:///{tmp_path}/var/youos.db"


def _seed_draft(db_url, **over):
    base = dict(
        message_id="m-in", thread_id="t1", account="you@example.com",
        sender="Alice <alice@x.com>", sender_email="alice@x.com",
        subject="Q3 plan", body="Can you confirm the Q3 plan?",
        received_at="2026-06-01T10:00:00Z", needs_reply_score=0.8, reasons=[],
        cold_outreach=False, tier="draft", draft="Yes, confirmed — see attached plan.",
        draft_model="qwen", draft_repairs=[], standing_instructions_snapshot=None,
    )
    base.update(over)
    return store.upsert_pending(db_url, **base)


def test_pairs_draft_with_real_send(db, monkeypatch):
    _seed_draft(db)
    threads = {"t1": {"messages": [
        _msg("m-in", "Alice <alice@x.com>", "Can you confirm the Q3 plan?"),
        _msg("m-reply", "You <you@example.com>",
             "Confirmed for the 18th with a 2-day buffer. Sending the plan now.\n\nOn Mon, Alice wrote:\n> Can you confirm"),
    ]}}
    monkeypatch.setattr("app.ingestion.adapters.get_google_source", lambda backend=None: _FakeSource(threads))

    r = outcome_capture.capture_send_outcomes(db, account="you@example.com")
    assert r["paired"] == 1 and r["scanned"] == 1
    assert r["avg_edit_distance"] is not None

    import sqlite3
    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    c.row_factory = sqlite3.Row
    fp = c.execute("SELECT * FROM feedback_pairs").fetchall()
    assert len(fp) == 1
    assert "Confirmed for the 18th" in fp[0]["edited_reply"]
    assert "> Can you confirm" not in fp[0]["edited_reply"]   # quoted history stripped
    assert fp[0]["generated_draft"] == "Yes, confirmed — see attached plan."
    assert fp[0]["edit_distance_pct"] is not None
    row = c.execute("SELECT outcome, outcome_captured FROM agent_pending_drafts").fetchone()
    assert row["outcome"] == "sent" and row["outcome_captured"] == 1
    c.close()


def test_no_send_marks_outcome_without_pair(db, monkeypatch):
    _seed_draft(db)
    # Thread has only the inbound + a follow-up from the sender — user never replied.
    threads = {"t1": {"messages": [
        _msg("m-in", "Alice <alice@x.com>", "Can you confirm the Q3 plan?"),
        _msg("m-bump", "Alice <alice@x.com>", "Just bumping this."),
    ]}}
    monkeypatch.setattr("app.ingestion.adapters.get_google_source", lambda backend=None: _FakeSource(threads))

    r = outcome_capture.capture_send_outcomes(db, account="you@example.com", no_send_after_days=0)
    assert r["paired"] == 0 and r["no_send"] == 1

    import sqlite3
    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    c.row_factory = sqlite3.Row
    assert c.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0] == 0
    assert c.execute("SELECT outcome FROM agent_pending_drafts").fetchone()[0] == "no_send"
    c.close()


def test_idempotent(db, monkeypatch):
    _seed_draft(db)
    threads = {"t1": {"messages": [
        _msg("m-in", "Alice <alice@x.com>", "Can you confirm the Q3 plan?"),
        _msg("m-reply", "You <you@example.com>", "Yes, that works for me, thanks."),
    ]}}
    monkeypatch.setattr("app.ingestion.adapters.get_google_source", lambda backend=None: _FakeSource(threads))
    first = outcome_capture.capture_send_outcomes(db, account="you@example.com")
    second = outcome_capture.capture_send_outcomes(db, account="you@example.com")
    assert first["paired"] == 1
    assert second["scanned"] == 0 and second["paired"] == 0


def test_recent_no_reply_left_pending(db, monkeypatch):
    _seed_draft(db)
    threads = {"t1": {"messages": [_msg("m-in", "Alice <alice@x.com>", "Can you confirm the Q3 plan?")]}}
    monkeypatch.setattr("app.ingestion.adapters.get_google_source", lambda backend=None: _FakeSource(threads))
    # Fresh row, no reply yet, generous no_send window → leave for a later run.
    r = outcome_capture.capture_send_outcomes(db, account="you@example.com", no_send_after_days=5)
    assert r["still_pending"] == 1 and r["paired"] == 0 and r["no_send"] == 0
