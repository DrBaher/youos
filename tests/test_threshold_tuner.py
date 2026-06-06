"""Auto-tune the needs-reply threshold from real send outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import store
from app.agent.threshold_tuner import (
    outcome_counts,
    recommend_from_database,
    recommend_threshold,
)

# --- Pure recommend_threshold logic ----------------------------------------


def test_holds_below_min_samples():
    r = recommend_threshold(current=0.60, sent=2, no_send=5, min_samples=25)
    assert not r.changed
    assert r.recommended == 0.60
    assert "need 25" in r.reason


def test_raises_when_over_drafting():
    # send rate ~12% (5 sent, 37 no_send) — the real prod signal. Over-drafting.
    r = recommend_threshold(current=0.60, sent=5, no_send=37)
    assert r.changed
    assert r.recommended == 0.65  # one step up
    assert "raising" in r.reason
    assert r.send_rate is not None and r.send_rate < 0.2


def test_lowers_when_under_drafting():
    # send rate ~83% — almost everything queued earned a reply; draft more.
    r = recommend_threshold(current=0.60, sent=25, no_send=5)
    assert r.changed
    assert r.recommended == 0.55  # one step down
    assert "lowering" in r.reason


def test_holds_within_dead_band():
    # send rate 40% == target → no change.
    r = recommend_threshold(current=0.60, sent=20, no_send=30)
    assert not r.changed
    assert r.recommended == 0.60
    assert "within" in r.reason


def test_clamps_at_ceiling():
    r = recommend_threshold(current=0.85, sent=2, no_send=40, bounds=(0.5, 0.85))
    assert not r.changed  # already at ceiling, can't raise further
    assert r.recommended == 0.85
    assert "ceiling" in r.reason


def test_clamps_at_floor():
    r = recommend_threshold(current=0.50, sent=40, no_send=2, bounds=(0.5, 0.85))
    assert not r.changed
    assert r.recommended == 0.50
    assert "floor" in r.reason


def test_one_step_at_a_time():
    # Even an extreme send rate moves only one step, never jumps to the bound.
    r = recommend_threshold(current=0.60, sent=0, no_send=100, step=0.05)
    assert r.recommended == 0.65


def test_samples_property():
    r = recommend_threshold(current=0.60, sent=10, no_send=15)
    assert r.samples == 25
    d = r.to_dict()
    assert d["samples"] == 25 and d["sent"] == 10 and d["no_send"] == 15


# --- DB-backed reads --------------------------------------------------------


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


def _seed(db_url, outcome, n, **over):
    for i in range(n):
        row_id = store.upsert_pending(
            db_url,
            message_id=f"m-{outcome}-{i}", thread_id=f"t-{outcome}-{i}",
            account="you@example.com", sender="Alice <alice@x.com>", sender_email="alice@x.com",
            subject="s", body="b", received_at="2026-06-01T10:00:00Z",
            needs_reply_score=0.8, reasons=[], cold_outreach=False,
            tier="draft", draft="d", draft_model="qwen", draft_repairs=[],
            standing_instructions_snapshot=None, **over,
        )
        import sqlite3
        c = sqlite3.connect(db_url.removeprefix("sqlite:///"))
        c.execute(
            "UPDATE agent_pending_drafts SET outcome = ?, outcome_captured = 1 WHERE id = ?",
            (outcome, row_id),
        )
        c.commit()
        c.close()


def test_outcome_counts_reads_decided(db):
    _seed(db, "sent", 5)
    _seed(db, "no_send", 37)
    sent, no_send = outcome_counts(db, account="you@example.com")
    assert sent == 5 and no_send == 37


def test_outcome_counts_missing_table_safe(tmp_path):
    # A nonexistent DB path → (0, 0), never raises.
    sent, no_send = outcome_counts(f"sqlite:///{tmp_path}/nope.db")
    assert (sent, no_send) == (0, 0)


def test_recommend_from_database_raises_on_low_send_rate(db):
    _seed(db, "sent", 5)
    _seed(db, "no_send", 37)
    r = recommend_from_database(db, current=0.60, account="you@example.com")
    assert r.changed and r.recommended == 0.65
