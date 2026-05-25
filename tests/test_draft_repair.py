"""Post-generation draft repair pass (Draft PR A).

The model output used to be returned with only an emptiness check. This pins
the repair helpers: a non-mutating length flag (always on), and the opt-in
(default-off) greeting/closing enforcement + trailing-signature strip.
"""

from __future__ import annotations

from app.generation import service as svc
from app.generation.service import (
    DraftResponse,
    _draft_has_closing,
    _draft_has_greeting,
    _get_repair_config,
    _length_flag,
    _repair_draft,
)

OFF = {"enforce_greeting_closing": False, "strip_trailing_signature": False}
ALL_ON = {"enforce_greeting_closing": True, "strip_trailing_signature": True}


# --- length flag (non-mutating, always on) ---------------------------------


def test_length_flag_none_without_target():
    assert _length_flag("a b c", None) is None
    assert _length_flag("a b c", 0) is None


def test_length_flag_ok_long_short():
    target = 40
    assert _length_flag(" ".join(["w"] * 40), target) == "ok"
    assert _length_flag(" ".join(["w"] * 200), target) == "long"   # > 2x
    assert _length_flag(" ".join(["w"] * 5), target) == "short"    # < target/2
    assert _length_flag("", target) is None                        # empty


# --- greeting / closing detection ------------------------------------------


def test_greeting_detection():
    assert _draft_has_greeting("Hi John,\n\nThanks for...", "Hi {name},")
    assert _draft_has_greeting("Hey there — sure thing", "Hi,")
    assert not _draft_has_greeting("Sure, that works for me.", "Hi John,")


def test_closing_detection():
    assert _draft_has_closing("...see you then.\n\nBest,\nBaher", "Best,")
    assert _draft_has_closing("...talk soon", "Cheers,")
    assert not _draft_has_closing("...let me know what you think.", "Best,\nBaher")


# --- repair: default-off is behavior-preserving ----------------------------


def test_repair_default_off_does_not_mutate():
    draft = "Sure, that works. I'll send the doc.\n\nBest,\nBaher"
    text, repairs, flag = _repair_draft(draft, greeting="Hi John,", closing="Best,\nBaher", target_words=40, config=OFF)
    assert text == draft
    assert repairs == []
    assert flag == "short"  # only the (harmless) length annotation


def test_repair_leaves_placeholder_drafts_untouched():
    placeholder = "[no model available: local model unavailable and fallback disabled]"
    text, repairs, flag = _repair_draft(placeholder, greeting="Hi,", closing="Best,", target_words=40, config=ALL_ON)
    assert text == placeholder
    assert repairs == []
    assert flag is None


# --- repair: opt-in mutations ----------------------------------------------


def test_strip_trailing_signature_when_enabled():
    draft = "Sounds good, let's do Tuesday.\n\nBest,\nBaher"
    cfg = {"enforce_greeting_closing": False, "strip_trailing_signature": True}
    text, repairs, _ = _repair_draft(draft, greeting="", closing="", target_words=None, config=cfg)
    assert "Baher" not in text
    assert text.strip() == "Sounds good, let's do Tuesday."
    assert "stripped_trailing_signature" in repairs


def test_enforce_greeting_and_closing_adds_when_missing():
    draft = "Sure, that works for me."
    cfg = {"enforce_greeting_closing": True, "strip_trailing_signature": False}
    text, repairs, _ = _repair_draft(draft, greeting="Hi John,", closing="Best,\nBaher", target_words=None, config=cfg)
    assert text.startswith("Hi John,")
    assert text.rstrip().endswith("Best,\nBaher")
    assert "added_greeting" in repairs and "added_closing" in repairs


def test_enforce_does_not_double_add_when_present():
    draft = "Hi John,\n\nSure, that works.\n\nBest,\nBaher"
    cfg = {"enforce_greeting_closing": True, "strip_trailing_signature": False}
    text, repairs, _ = _repair_draft(draft, greeting="Hi John,", closing="Best,\nBaher", target_words=None, config=cfg)
    assert text == draft
    assert repairs == []


# --- config reader ----------------------------------------------------------


def test_repair_config_defaults_off(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    assert _get_repair_config() == {"enforce_greeting_closing": False, "strip_trailing_signature": False}


def test_repair_config_reads_flags(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"generation": {"repair": {"enforce_greeting_closing": True, "strip_trailing_signature": True}}},
    )
    assert _get_repair_config() == {"enforce_greeting_closing": True, "strip_trailing_signature": True}


# --- response shape ---------------------------------------------------------


def test_draft_response_exposes_repair_fields():
    resp = DraftResponse(
        draft="hi", detected_mode="work", precedent_used=[], retrieval_method="fts",
        confidence="low", confidence_reason="x", model_used="qwen2.5-1.5b-base",
        length_flag="ok", repairs=["added_greeting"],
    )
    d = resp.to_dict()
    assert d["length_flag"] == "ok"
    assert d["repairs"] == ["added_greeting"]
    # default instance has empty repairs / no flag (back-compat)
    assert svc.DraftResponse(
        draft="x", detected_mode="m", precedent_used=[], retrieval_method="r",
        confidence="low", confidence_reason="c", model_used="none",
    ).repairs == []
