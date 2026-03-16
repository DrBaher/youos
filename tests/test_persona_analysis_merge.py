"""Tests for persona analysis merge (Item 2)."""

from __future__ import annotations

import yaml

from scripts.analyze_persona_merge import _dominant_pattern, merge_persona_analysis


def test_dominant_pattern_above_threshold():
    counter = {"Hi X": 70, "Hey X": 20, "Hello X": 10}
    assert _dominant_pattern(counter, 0.60) == "Hi X"


def test_dominant_pattern_below_threshold():
    counter = {"Hi X": 40, "Hey X": 35, "Hello X": 25}
    assert _dominant_pattern(counter, 0.60) is None


def test_dominant_pattern_empty():
    assert _dominant_pattern({}) is None


def test_merge_updates_avg_reply_words(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({
        "style": {"avg_reply_words": 40, "voice": "direct"},
        "greeting_patterns": {"default": "Hi,"},
        "closing_patterns": {"default": "Best,"},
    }))
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 55.0},
        "greeting_patterns": {"Hi X": 30, "Hey X": 70},
        "closing_patterns": {"Cheers": 50, "Best": 50},
    }

    changes = merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        findings_dict=findings,
    )

    assert any("avg_reply_words" in c for c in changes)
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["style"]["avg_reply_words"] == 55


def test_merge_skips_small_avg_change(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({
        "style": {"avg_reply_words": 40},
        "greeting_patterns": {},
        "closing_patterns": {},
    }))
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 43.0},
        "greeting_patterns": {},
        "closing_patterns": {},
    }

    changes = merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        findings_dict=findings,
    )

    assert not any("avg_reply_words" in c for c in changes)


def test_merge_updates_greeting_pattern(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({
        "style": {"avg_reply_words": 40},
        "greeting_patterns": {"default": "Hi,"},
        "closing_patterns": {"default": "Best,"},
    }))
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 40.0},
        "greeting_patterns": {"Hey X": 80, "Hi X": 20},
        "closing_patterns": {"Best": 50, "Cheers": 50},
    }

    changes = merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        findings_dict=findings,
    )

    assert any("greeting" in c for c in changes)
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["greeting_patterns"]["default"] == "Hey X"


def test_merge_dry_run_does_not_write(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    original = yaml.dump({"style": {"avg_reply_words": 40}, "greeting_patterns": {}, "closing_patterns": {}})
    persona_path.write_text(original)
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 100.0},
        "greeting_patterns": {},
        "closing_patterns": {},
    }

    changes = merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        dry_run=True,
        findings_dict=findings,
    )

    assert len(changes) > 0
    assert persona_path.read_text() == original
    assert not log_path.exists()


def test_merge_writes_log(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({
        "style": {"avg_reply_words": 40},
        "greeting_patterns": {},
        "closing_patterns": {},
    }))
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 100.0},
        "greeting_patterns": {},
        "closing_patterns": {},
    }

    merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        findings_dict=findings,
    )

    assert log_path.exists()
    content = log_path.read_text()
    assert "avg_reply_words" in content


def test_merge_preserves_custom_constraints(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({
        "style": {"avg_reply_words": 40, "constraints": ["custom rule"]},
        "greeting_patterns": {},
        "closing_patterns": {},
        "custom_constraints": ["never use emojis"],
    }))
    log_path = tmp_path / "merge.log"

    findings = {
        "reply_length": {"avg_words": 100.0},
        "greeting_patterns": {},
        "closing_patterns": {},
    }

    merge_persona_analysis(
        persona_path=persona_path,
        log_path=log_path,
        findings_dict=findings,
    )

    updated = yaml.safe_load(persona_path.read_text())
    assert updated["custom_constraints"] == ["never use emojis"]
    assert updated["style"]["constraints"] == ["custom rule"]
