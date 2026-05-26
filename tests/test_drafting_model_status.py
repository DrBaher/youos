"""The drafting-model reality signal — to prevent silent LoRA failures.

A user must be able to tell when drafts actually use their LoRA vs. silently
run on the base model or fall back to the cloud. These pin the classifier
(reality from draft_events first, capability as fallback), the by_model
aggregate, and the doctor warning that fires when the LoRA isn't really in use.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.stats import (
    _classify_drafting,
    _classify_model_used,
    get_drafting_model_status,
)

# --- bucketing -------------------------------------------------------------


def test_classify_model_used_buckets():
    assert _classify_model_used("qwen2.5-1.5b-lora") == "lora"
    assert _classify_model_used("qwen2.5-1.5b-lora-internal") == "lora"  # per-persona
    assert _classify_model_used("qwen2.5-1.5b-base") == "base"
    assert _classify_model_used("claude") == "cloud"
    assert _classify_model_used("ollama:mistral") == "cloud"
    assert _classify_model_used("") == "other"


# --- classifier: reality (recent drafts) -----------------------------------


def test_all_lora_is_healthy_personalized():
    state, label, _detail, healthy = _classify_drafting({"qwen2.5-1.5b-lora": 30}, True, True)
    assert state == "personalized" and healthy is True


def test_base_only_is_unhealthy():
    state, _label, detail, healthy = _classify_drafting({"qwen2.5-1.5b-base": 12}, False, True)
    assert state == "base" and healthy is False
    assert "base model" in detail.lower()


def test_cloud_fallback_is_unhealthy():
    state, _label, _detail, healthy = _classify_drafting({"claude": 8}, True, True)
    assert state == "cloud" and healthy is False


def test_mixed_lora_and_fallback_is_flagged():
    state, _label, _detail, healthy = _classify_drafting(
        {"qwen2.5-1.5b-lora": 18, "claude": 2}, True, True
    )
    assert state == "mixed" and healthy is False


# --- classifier: capability fallback (no drafts yet) -----------------------


def test_no_drafts_adapter_ready_is_healthy():
    state, _label, _detail, healthy = _classify_drafting({}, adapter_trained=True, local_available=True)
    assert state == "personalized" and healthy is True


def test_no_drafts_no_adapter_warns_base():
    state, _label, _detail, healthy = _classify_drafting({}, adapter_trained=False, local_available=True)
    assert state == "base" and healthy is False


def test_no_drafts_no_mlx_warns_cloud():
    state, _label, detail, healthy = _classify_drafting({}, adapter_trained=True, local_available=False)
    assert state == "cloud" and healthy is False
    assert "mlx_lm" in detail


# --- end-to-end against a temp DB ------------------------------------------


def _seed_draft_events(db_path: Path, models: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE draft_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL, model_used TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.executemany(
        "INSERT INTO draft_events(inbound_text, generated_draft, model_used) VALUES('in','out',?)",
        [(m,) for m in models],
    )
    conn.commit()
    conn.close()


def test_get_drafting_model_status_reads_recent_drafts(tmp_path, monkeypatch):
    db = tmp_path / "youos.db"
    _seed_draft_events(db, ["claude", "claude", "claude"])
    # Adapter present + mlx available, but recent drafts all fell back to Claude:
    # reality must win over capability and flag the fallback.
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: tmp_path / "no_adapter")
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/mlx_lm")

    status = get_drafting_model_status(f"sqlite:///{db}")
    assert status["state"] == "cloud"
    assert status["healthy"] is False
    assert status["recent_by_model"] == {"claude": 3}


def test_by_model_in_summarize_draft_events(tmp_path):
    from app.core.stats import summarize_draft_events

    db = tmp_path / "youos.db"
    _seed_draft_events(db, ["qwen2.5-1.5b-lora", "qwen2.5-1.5b-lora", "claude"])
    summary = summarize_draft_events(f"sqlite:///{db}")
    assert summary["by_model"]["qwen2.5-1.5b-lora"] == 2
    assert summary["by_model"]["claude"] == 1


# --- doctor warning --------------------------------------------------------


def test_doctor_warns_when_lora_not_in_use(monkeypatch):
    from app.core import doctor

    monkeypatch.setattr(
        doctor, "run_doctor_checks", lambda: (True, [])
    )
    # Force the drafting signal to "unhealthy base" via the stats helper.
    monkeypatch.setattr(
        "app.core.stats.get_drafting_model_status",
        lambda _u: {"healthy": False, "label": "Base model — no LoRA trained yet", "detail": "No adapter trained yet."},
    )
    _passed, _failures, warnings = doctor.run_doctor_checks_full()
    assert any("Drafting:" in w and "Base model" in w for w in warnings)
