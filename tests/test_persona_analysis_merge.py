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
    persona_path.write_text(
        yaml.dump(
            {
                "style": {"avg_reply_words": 40, "voice": "direct"},
                "greeting_patterns": {"default": "Hi,"},
                "closing_patterns": {"default": "Best,"},
            }
        )
    )
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
    persona_path.write_text(
        yaml.dump(
            {
                "style": {"avg_reply_words": 40},
                "greeting_patterns": {},
                "closing_patterns": {},
            }
        )
    )
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
    """The analyzer emits *category labels* ("Hey X"); the merge must translate
    them to renderable phrases ("Hey {name},") before writing persona.yaml.
    Previously the literal label was copied in, so the generator emitted
    "Hey X," verbatim — the merge's category-translation map is the fix."""
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        yaml.dump(
            {
                "style": {"avg_reply_words": 40},
                "greeting_patterns": {"default": "Hi,"},
                "closing_patterns": {"default": "Best,"},
            }
        )
    )
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
    # Translated, not the literal category label.
    assert updated["greeting_patterns"]["default"] == "Hey {name},"


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
    persona_path.write_text(
        yaml.dump(
            {
                "style": {"avg_reply_words": 40},
                "greeting_patterns": {},
                "closing_patterns": {},
            }
        )
    )
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
    persona_path.write_text(
        yaml.dump(
            {
                "style": {"avg_reply_words": 40, "constraints": ["custom rule"]},
                "greeting_patterns": {},
                "closing_patterns": {},
                "custom_constraints": ["never use emojis"],
            }
        )
    )
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


# --- Category-label translation (Statement/Direct-start/unknown categories) ---
# Regressions for the QA-review bug: the merge used to copy the analyzer's
# *category labels* into persona.yaml verbatim, so a corpus dominated by
# no-signoff replies would write `closing default: "Statement"` and the
# generator then emitted the literal word "Statement" as the closing.


def test_merge_translates_statement_category_to_empty_closing(tmp_path):
    """Corpus dominated by no-signoff replies ('Statement' >60%) must collapse
    the default closing to '' (no closing phrase), not write 'Statement' as a
    literal closing string."""
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        yaml.dump({"style": {}, "closing_patterns": {"default": "Best,"}})
    )
    findings = {"closing_patterns": {"Statement": 70, "Thanks": 20, "Question": 10}}
    merge_persona_analysis(
        persona_path=persona_path,
        log_path=tmp_path / "m.log",
        findings_dict=findings,
    )
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["closing_patterns"]["default"] == ""


def test_merge_translates_direct_start_to_empty_greeting(tmp_path):
    """'Direct start' (no greeting) translates to ''. Don't write the category
    label as a literal greeting prefix."""
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        yaml.dump({"style": {}, "greeting_patterns": {"default": "Hi,"}})
    )
    findings = {"greeting_patterns": {"Direct start": 70, "Hi X": 30}}
    merge_persona_analysis(
        persona_path=persona_path,
        log_path=tmp_path / "m.log",
        findings_dict=findings,
    )
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["greeting_patterns"]["default"] == ""


def test_merge_translates_hi_x_category_to_hi_name_phrase(tmp_path):
    """'Hi X' → 'Hi {name},' — the renderable form the prompt actually uses."""
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        yaml.dump({"style": {}, "greeting_patterns": {"default": "Hello,"}})
    )
    findings = {"greeting_patterns": {"Hi X": 80, "Hey X": 20}}
    merge_persona_analysis(
        persona_path=persona_path,
        log_path=tmp_path / "m.log",
        findings_dict=findings,
    )
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["greeting_patterns"]["default"] == "Hi {name},"


def test_merge_skips_unknown_category_rather_than_copying_garbage(tmp_path):
    """If the analyzer emits a category we don't recognise yet, the merge
    leaves persona.yaml unchanged for that field — better than corrupting it."""
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        yaml.dump({"style": {}, "closing_patterns": {"default": "Best,"}})
    )
    findings = {"closing_patterns": {"BrandNewCategory": 80, "Statement": 20}}
    merge_persona_analysis(
        persona_path=persona_path,
        log_path=tmp_path / "m.log",
        findings_dict=findings,
    )
    updated = yaml.safe_load(persona_path.read_text())
    assert updated["closing_patterns"]["default"] == "Best,"


def test_translate_category_helper_direct_lookups():
    from scripts.analyze_persona_merge import _translate_category

    assert _translate_category("Hi X", kind="greeting") == "Hi {name},"
    assert _translate_category("Direct start", kind="greeting") == ""
    assert _translate_category("Statement", kind="closing") == ""
    assert _translate_category("Thanks", kind="closing") == "Thanks,"
    assert _translate_category("UnknownXYZ", kind="closing") is None
