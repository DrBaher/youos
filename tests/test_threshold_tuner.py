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


# --- Cross-process hot-reload (nightly writes config; server must pick it up) ---


def test_get_agent_config_hot_reloads_external_threshold_write(tmp_path, monkeypatch):
    """A config write by ANOTHER process (the nightly auto-tune) must take
    effect on the running server without a restart — get_agent_config drops the
    stale load_config cache when the file's mtime changes.

    The second write goes straight to the file (NOT via save_config, which
    clears the cache in-process) to faithfully simulate a separate process: the
    server's own load_config cache stays warm, so only reload_config_if_changed
    can surface the new value."""
    import os

    import yaml

    import app.core.config as cfgmod
    from app.agent.scheduler import get_agent_config
    from app.core.config import load_config

    cfg_path = tmp_path / "youos_config.yaml"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(cfgmod, "_config_mtime", None)
    load_config.cache_clear()

    cfg_path.write_text(yaml.safe_dump({"agent": {"threshold": 0.60}}))
    # Warm the in-process cache exactly as a running server would.
    assert get_agent_config()["threshold"] == 0.60

    # Separate process rewrites the file directly — no cache_clear reaches us.
    cfg_path.write_text(yaml.safe_dump({"agent": {"threshold": 0.65}}))
    st = cfg_path.stat()
    os.utime(cfg_path, (st.st_atime + 5, st.st_mtime + 5))  # ensure mtime differs

    # Only the mtime-based hot-reload can make the warm-cache server see 0.65.
    assert get_agent_config()["threshold"] == 0.65


# --- Recency window + since-filter (b228) -----------------------------------
#
# Forensic finding (baheros, 2026-06-11): with a 60-day window and no
# change-awareness, ~95 no_send outcomes from drafts queued under OLD
# thresholds kept reading as "over-drafting" for weeks after the tuner had
# already raised the threshold — so it marched straight to the 0.85 ceiling
# while the post-change send rate was actually 62%.


def _backdate(db_url, *, outcome, days):
    import sqlite3

    c = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    c.execute(
        f"UPDATE agent_pending_drafts SET created_at = datetime('now', '-{int(days)} days') "
        "WHERE outcome = ?",
        (outcome,),
    )
    c.commit()
    c.close()


def _iso_days_ago(days):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def test_outcome_counts_since_filters_pre_change_drafts(db):
    _seed(db, "no_send", 10)
    _backdate(db, outcome="no_send", days=5)
    _seed(db, "sent", 3)

    sent, no_send = outcome_counts(db, account="you@example.com", since=_iso_days_ago(1))
    assert (sent, no_send) == (3, 0)
    # Without since, both cohorts are visible (they're inside the days window).
    sent, no_send = outcome_counts(db, account="you@example.com")
    assert (sent, no_send) == (3, 10)


def test_outcome_counts_default_window_excludes_stale_outcomes(db):
    _seed(db, "no_send", 30)
    _backdate(db, outcome="no_send", days=30)

    assert outcome_counts(db, account="you@example.com") == (0, 0)


def test_recommend_from_database_holds_when_evidence_predates_change(db):
    # 37 unanswered drafts from BEFORE the threshold last moved must not push
    # it further; the 2 fresh outcomes alone are below the sample floor.
    _seed(db, "no_send", 37)
    _backdate(db, outcome="no_send", days=5)
    _seed(db, "sent", 2)

    r = recommend_from_database(db, current=0.80, account="you@example.com", since=_iso_days_ago(1))
    assert not r.changed
    assert r.samples == 2


def test_step_tune_threshold_stamps_changed_at(db, monkeypatch):
    import yaml

    import app.core.config as cfgmod

    cfgmod.CONFIG_PATH.write_text(yaml.safe_dump({"agent": {"threshold": 0.60}}))
    cfgmod.load_config.cache_clear()
    monkeypatch.setattr(cfgmod, "_config_mtime", None)
    _seed(db, "no_send", 30)

    from scripts.nightly_pipeline import step_tune_threshold

    out = step_tune_threshold()
    assert out == "tuned 0.60->0.65"
    data = yaml.safe_load(cfgmod.CONFIG_PATH.read_text())
    assert data["agent"]["threshold"] == 0.65
    assert data["agent"]["threshold_changed_at"]


def test_step_tune_threshold_ignores_pre_change_outcomes(db, monkeypatch):
    import yaml

    import app.core.config as cfgmod

    cfgmod.CONFIG_PATH.write_text(
        yaml.safe_dump({"agent": {"threshold": 0.80, "threshold_changed_at": _iso_days_ago(1)}})
    )
    cfgmod.load_config.cache_clear()
    monkeypatch.setattr(cfgmod, "_config_mtime", None)
    _seed(db, "no_send", 30)
    _backdate(db, outcome="no_send", days=3)

    from scripts.nightly_pipeline import step_tune_threshold

    out = step_tune_threshold()
    assert out.startswith("held at 0.80")
    assert yaml.safe_load(cfgmod.CONFIG_PATH.read_text())["agent"]["threshold"] == 0.80


def test_set_flag_threshold_stamps_changed_at(tmp_path):
    import yaml

    from app.core.feature_flags import set_flag

    cfg_path = tmp_path / "youos_config.yaml"
    set_flag("agent.threshold", 0.7, config_path=cfg_path)
    data = yaml.safe_load(cfg_path.read_text())
    assert data["agent"]["threshold"] == 0.7
    first_stamp = data["agent"]["threshold_changed_at"]
    assert first_stamp

    # Re-writing the SAME value must not move the stamp (no fake evidence reset).
    set_flag("agent.threshold", 0.7, config_path=cfg_path)
    assert yaml.safe_load(cfg_path.read_text())["agent"]["threshold_changed_at"] == first_stamp


def test_set_flag_other_keys_do_not_stamp(tmp_path):
    import yaml

    from app.core.feature_flags import set_flag

    cfg_path = tmp_path / "youos_config.yaml"
    set_flag("agent.enabled", True, config_path=cfg_path)
    assert "threshold_changed_at" not in yaml.safe_load(cfg_path.read_text()).get("agent", {})
