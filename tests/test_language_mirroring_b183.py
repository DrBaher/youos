"""b183 — drafts must mirror the inbound's language.

Root cause: the b173 chat refactor kept a language block but gated it on a
positive non-English detection (``language_hint != "en"``). When the cheap
heuristic under-detected short/informal German, the instruction vanished and the
local model drafted in English. These tests pin:

* ``detect_language`` now catches the live-demo German inbound (was misread as
  "en"),
* the ChatML system turn ALWAYS carries a language-mirroring directive and names
  the language when known,
* the legacy flat prompt does the same,
* ``verify_draft`` flags a German-inbound + English-draft mismatch (blocking) and
  passes a German-inbound + German-draft.
"""

from __future__ import annotations

from app.core.text_utils import detect_language, language_name
from app.generation.service import (
    _assemble_system_text,
    _language_instruction,
    assemble_prompt,
)
from app.generation.verify import verify_draft

# The exact live-demo German inbound that regressed to an English draft.
DEMO_GERMAN = (
    "Hallo Baher, ich wollte einmal freundlichst nachhorchen, "
    "ob es schon Neuigkeiten gibt."
)
FORMAL_GERMAN = (
    "Sehr geehrter Herr Müller,\n\n"
    "ich möchte Sie gerne zu einem Gespräch über die geplante "
    "Zusammenarbeit einladen. Wären Sie nächste Woche verfügbar?\n\n"
    "Mit freundlichen Grüßen,\nAnna Schmidt"
)


# --- detection ---------------------------------------------------------------

def test_detect_language_demo_german():
    """The live-demo informal German inbound must detect as German, not en."""
    assert detect_language(DEMO_GERMAN) == "de"


def test_detect_language_formal_german():
    assert detect_language(FORMAL_GERMAN) == "de"


def test_detect_language_french_still_works():
    assert detect_language(
        "Bonjour Baher, je voulais savoir si vous avez des nouvelles."
    ) == "fr"


def test_detect_language_english_not_misdetected():
    assert detect_language(
        "Hi Baher, just checking in to see if there is any news yet."
    ) == "en"


def test_language_name_mapping():
    assert language_name("de") == "German"
    assert language_name("fr") == "French"
    assert language_name(None) is None
    assert language_name("zz") is None


# --- instruction builder -----------------------------------------------------

def test_language_instruction_names_known_language():
    instr = _language_instruction("de")
    assert "Reply in German." in instr
    assert "same language" in instr.lower()


def test_language_instruction_always_present_for_english():
    """Even when detection says English, a mirror directive is still produced —
    so a misdetect can never drop the rule."""
    instr = _language_instruction("en")
    assert instr.strip()
    assert "same language" in instr.lower()
    # English is not named explicitly (no "Reply in English.") — the generic
    # mirror rule covers it without nudging the model toward translating.
    assert "Reply in English." not in instr


def test_language_instruction_handles_none():
    instr = _language_instruction(None)
    assert instr.strip()
    assert "same language" in instr.lower()


# --- system turn (b173 ChatML path) ------------------------------------------

def _system_text(language_hint, inbound):
    return _assemble_system_text(
        persona={"style": {"voice": "direct"}},
        prompts={},
        detected_mode=None,
        audience_hint=None,
        tone_hint=None,
        sender_context=None,
        language_hint=language_hint,
        intent_hint=None,
        sender_type=None,
        first_name=None,
        memory_facts=None,
        user_prompt=None,
        extra_constraint=None,
        inbound_message=inbound,
        include_exemplar_hint=False,
    )


def test_system_turn_contains_language_mirror_instruction():
    text = _system_text("de", DEMO_GERMAN)
    assert "Reply in German." in text
    assert "same language" in text.lower()


def test_system_turn_has_mirror_even_when_detected_english():
    """The regression: when language_hint=='en' the directive used to vanish."""
    text = _system_text("en", "Hi, any news on the proposal?")
    assert "same language" in text.lower()


def test_system_turn_courtesy_rule_still_present():
    """b179 courtesy rule must coexist with the language block."""
    text = _system_text("de", DEMO_GERMAN)
    assert "courteous" in text.lower()


# --- legacy flat prompt ------------------------------------------------------

def test_legacy_prompt_contains_language_instruction():
    prompt = assemble_prompt(
        inbound_message=DEMO_GERMAN,
        reply_pairs=[],
        persona={"style": {"voice": "direct"}},
        prompts={},
        language_hint="de",
    )
    assert "Reply in German." in prompt


# --- verify-before-accept ----------------------------------------------------

def test_verify_flags_german_inbound_english_draft():
    vr = verify_draft(
        "Hi, thanks for reaching out. I'll get back to you next week.",
        inbound=DEMO_GERMAN,
    )
    assert not vr.ok
    assert any("language mismatch" in b for b in vr.blocking)


def test_verify_passes_german_inbound_german_draft():
    vr = verify_draft(
        "Hallo, vielen Dank für Ihre Nachricht. Ich melde mich nächste Woche bei Ihnen.",
        inbound=DEMO_GERMAN,
    )
    assert not any("language mismatch" in b for b in vr.blocking)


def test_verify_passes_matching_english():
    vr = verify_draft(
        "Thanks for the note. I'll review the proposal and reply shortly.",
        inbound="Hi, did you get a chance to review the proposal I sent?",
    )
    assert not any("language mismatch" in b for b in vr.blocking)
