"""b241: state files are written atomically (temp + fsync + os.replace).

A torn pipeline_last_run.json silently drops golden_composite (the
adapter-promotion gate then auto-promotes as "cold start"); a torn
persona.yaml strips every style pattern; a torn train.jsonl gets trained on.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app.core.atomic_io import atomic_write_json, atomic_write_text


def test_atomic_write_text_replaces_and_keeps_0644(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("old")
    atomic_write_text(f, "new")
    assert f.read_text() == "new"
    assert oct(stat.S_IMODE(os.stat(f).st_mode)) == "0o644"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_failure_leaves_original_and_no_droppings(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    atomic_write_json(f, {"golden_composite": 0.42})

    def exploding_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError):
        atomic_write_json(f, {"golden_composite": 0.5})
    monkeypatch.undo()

    assert json.loads(f.read_text())["golden_composite"] == 0.42
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_pipeline_log_writer_is_atomic(tmp_path, monkeypatch):
    """_write_pipeline_log and _save_last_auto_feedback_at preserve existing
    keys and produce parseable JSON via the atomic helper."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    import scripts.nightly_pipeline as np

    monkeypatch.setattr(np, "_pipeline_log_path", lambda: tmp_path / "var" / "pipeline_last_run.json")

    np._write_pipeline_log({"status": "ok", "golden_composite": 0.37})
    np._save_last_auto_feedback_at()
    data = json.loads((tmp_path / "var" / "pipeline_last_run.json").read_text())
    assert data["golden_composite"] == 0.37  # baseline survives the second writer
    assert "last_auto_feedback_at" in data


def test_strip_curriculum_line_rewrites_atomically(tmp_path):
    from scripts.finetune_lora import strip_curriculum_line

    train = tmp_path / "train.jsonl"
    rows = ['{"_curriculum": {"warmup": 3}}', '{"text": "a"}', '{"text": "b"}']
    train.write_text("\n".join(rows) + "\n")
    assert strip_curriculum_line(train) is True
    assert train.read_text() == '{"text": "a"}\n{"text": "b"}\n'
    assert strip_curriculum_line(train) is False  # idempotent
    assert [p.name for p in tmp_path.iterdir()] == ["train.jsonl"]
