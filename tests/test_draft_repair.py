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

OFF = {
    "enforce_greeting_closing": False,
    "strip_trailing_signature": False,
    "strip_quote_tail": False,
    "decode_html_entities": False,
}
ALL_ON = {
    "enforce_greeting_closing": True,
    "strip_trailing_signature": True,
    "strip_quote_tail": True,
    "decode_html_entities": True,
}


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


def test_repair_config_defaults(monkeypatch):
    """With no config: enforce_greeting_closing is opt-in (False); the three
    artifact-removal repairs default True."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    cfg = _get_repair_config()
    assert cfg["enforce_greeting_closing"] is False
    assert cfg["strip_trailing_signature"] is True
    assert cfg["strip_quote_tail"] is True
    assert cfg["decode_html_entities"] is True


def test_repair_config_explicit_overrides(monkeypatch):
    """Per-instance YAML can override any flag (turn artifact repairs OFF, or
    flip greeting/closing enforcement ON)."""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {
            "generation": {
                "repair": {
                    "enforce_greeting_closing": True,
                    "strip_trailing_signature": False,
                    "strip_quote_tail": False,
                    "decode_html_entities": False,
                }
            }
        },
    )
    cfg = _get_repair_config()
    assert cfg["enforce_greeting_closing"] is True
    assert cfg["strip_trailing_signature"] is False
    assert cfg["strip_quote_tail"] is False
    assert cfg["decode_html_entities"] is False


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


# --- regressions for run-on signature / quote-tail / HTML entities ---------
# QA review (BaherOS) caught all three artifacts in real LoRA output: the model
# emits a run-on signature inline, hallucinates an email-quote tail, and leaves
# HTML entities undecoded. These pin the fixes.


def test_strip_signature_handles_run_on_inline_role_company():
    """The LoRA emits 'Cheers, Baher Al Hakim CEO / Work AI w: work.example'
    as a single line. The line-anchored ^Cheers,$ patterns miss this; the
    inline role+slash+capital and `w: URL` patterns catch it."""
    from app.generation.service import strip_signature

    draft = "Sure, let's chat. Cheers, Baher Al Hakim CEO / Work AI w: work.example e: baher@work.example"
    out = strip_signature(draft)
    assert "CEO" not in out
    assert "work.example" not in out
    assert "Cheers, Baher Al Hakim" in out  # closing + name kept


def test_strip_quote_tail_drops_email_quote_artifact():
    """The 'On <date>, <X> wrote:' hallucination — quote-tail leakage from the
    LoRA's training data — must be truncated, including anything after."""
    from app.generation.service import strip_quote_tail

    draft = (
        "Sure, sounds good — see you then. Thanks, Baher.\n\n"
        "On 23. Jul 2025 at 10:17 +0200, Baher Al Hakim <baher@baheros.com> wrote:\n"
        "Hey, I can do that. Let me know if you want to go to a restaurant or not."
    )
    out = strip_quote_tail(draft)
    assert "wrote:" not in out
    assert "On 23. Jul" not in out
    assert "Thanks, Baher." in out


def test_decode_html_entities_unescapes_common_artifacts():
    from app.generation.service import decode_html_entities

    assert decode_html_entities("I&#39;d love that &amp; can do it.") == "I'd love that & can do it."
    assert decode_html_entities("plain text") == "plain text"


def test_get_repair_config_defaults_strip_artifacts_on():
    """The three artifact-removal repairs default True now (signature,
    quote-tail, HTML entities) — they catch training-data leakage the user
    never wants. enforce_greeting_closing stays opt-in (it ADDS content)."""
    cfg = _get_repair_config()
    assert cfg["strip_trailing_signature"] is True
    assert cfg["strip_quote_tail"] is True
    assert cfg["decode_html_entities"] is True
    assert cfg["enforce_greeting_closing"] is False


def test_repair_pipeline_clears_all_three_artifacts_in_one_pass():
    """End-to-end: a draft with run-on signature, quote tail, and HTML entity
    comes out clean after a single _repair_draft pass with ALL_ON."""
    dirty = (
        "Hi Alex, I&#39;d be happy to help with that. Cheers, Baher Al Hakim CEO / Work AI w: work.example\n\n"
        "On 23. Jul 2025 at 10:17 +0200, Baher Al Hakim <baher@baheros.com> wrote: previous\n"
    )
    text, repairs, _flag = _repair_draft(
        dirty,
        greeting="Hi {name},",
        closing="Cheers,",
        target_words=40,
        config=ALL_ON,
    )
    assert "&#39;" not in text
    assert "wrote:" not in text
    assert "CEO" not in text
    assert "work.example" not in text
    assert "Hi Alex, I'd be happy to help with that." in text
    # Each pass logs what it did, so the operator can audit mutations.
    assert "stripped_quote_tail" in repairs
    assert "stripped_trailing_signature" in repairs
    assert "decoded_html_entities" in repairs


# --- Trailing user-name strip (BaherOS QA regression) ---------------------
# `strip_signature` removes contact details (`CEO / Work AI w: …`) but
# leaves a trailing `Baher Al Hakim` because it's not at line start. The LoRA
# learns "[brief content] + [name]" and on short queries emits only the name
# half. These pin the two-step exemplar/output strip.


def test_strip_trailing_user_name_after_punctuation(monkeypatch):
    monkeypatch.setattr("app.generation.service.get_user_names", lambda: ["Baher"])
    from app.generation.service import _strip_trailing_user_name as f

    assert f("Awesome! Delivery before holiday. Baher Al Hakim") == "Awesome! Delivery before holiday."
    assert f("Thanks, Baher Al Hakim") == "Thanks,"
    assert f("Sure, will move the call. Thanks, Baher") == "Sure, will move the call. Thanks,"


def test_strip_trailing_user_name_leaves_mid_sentence_use(monkeypatch):
    """Lookbehind for a sentence-ending punct + lookahead refusing further
    `.!?` until EOF means a Baher used mid-sentence isn't stripped."""
    monkeypatch.setattr("app.generation.service.get_user_names", lambda: ["Baher"])
    from app.generation.service import _strip_trailing_user_name as f

    assert f("Baher mentioned the team should ship by Friday.") == "Baher mentioned the team should ship by Friday."
    assert f("Sure, Baher said yes. Thanks.") == "Sure, Baher said yes. Thanks."


def test_strip_exemplar_signature_handles_run_on_plus_trailing_name(monkeypatch):
    """Combined: contact-detail block (via inline patterns) + trailing user
    name (via lookbehind). The kind of output the BaherOS LoRA produces."""
    monkeypatch.setattr("app.generation.service.get_user_names", lambda: ["Baher"])
    from app.generation.service import strip_exemplar_signature

    out = strip_exemplar_signature(
        "Awesome! Delivery before holiday. Baher Al Hakim CEO / Work AI w: work.example"
    )
    assert out == "Awesome! Delivery before holiday."


def test_repair_strip_trailing_signature_now_catches_trailing_name(monkeypatch):
    """`_repair_draft` with `strip_trailing_signature=True` runs both passes,
    so the LoRA emitting `… Baher Al Hakim` at the end of a draft is
    truncated cleanly (was previously left intact)."""
    monkeypatch.setattr("app.generation.service.get_user_names", lambda: ["Baher"])
    from app.generation.service import _repair_draft

    draft = "Sure, let's schedule a call next week. Let me know what works. Baher Al Hakim"
    cfg = {
        "enforce_greeting_closing": False,
        "strip_trailing_signature": True,
        "strip_quote_tail": False,
        "decode_html_entities": False,
    }
    text, repairs, _ = _repair_draft(
        draft, greeting="", closing="", target_words=None, config=cfg,
    )
    assert "Baher Al Hakim" not in text
    assert text.strip().endswith("Let me know what works.")
    assert "stripped_trailing_signature" in repairs


# --- multilingual greeting/closing awareness (b231) ---------------------------
# Live finding (2026-06-11): a German draft opening "Liebe Amina," wasn't
# recognized as a greeting, so enforce_greeting_closing prepended "Hey," on
# top — a double greeting — and closed an informal mail with the English
# formal closing. Non-English greetings/closings are now recognized, and the
# (English) persona greeting/closing are never added to a non-English draft.

_ENFORCE = {
    "enforce_greeting_closing": True,
    "strip_trailing_signature": False,
    "strip_quote_tail": False,
    "decode_html_entities": False,
}


def test_german_greeting_recognized_no_double_greeting():
    draft = "Liebe Amina,\n\ndie Formulare sind erhalten – danke!\n\nMit freundlichen Grüßen"
    text, repairs, _ = svc._repair_draft(
        draft, greeting="Hey,", closing="Best,\nBaher", target_words=None, config=_ENFORCE,
    )
    assert "added_greeting" not in repairs
    assert "added_closing" not in repairs
    assert text == draft


def test_german_draft_without_greeting_gets_no_english_prepend():
    draft = "die Unterlagen sind angekommen und ich melde mich nächste Woche bei dir."
    text, repairs, _ = svc._repair_draft(
        draft, greeting="Hey,", closing="Best,\nBaher", target_words=None, config=_ENFORCE,
    )
    assert "added_greeting" not in repairs
    assert not text.startswith("Hey,")


def test_english_draft_still_gets_greeting_and_closing():
    draft = "Confirmed for Thursday — see you then."
    text, repairs, _ = svc._repair_draft(
        draft, greeting="Hi Alice,", closing="Best,\nBaher", target_words=None, config=_ENFORCE,
    )
    assert "added_greeting" in repairs and "added_closing" in repairs
    assert text.startswith("Hi Alice,") and text.rstrip().endswith("Baher")


def test_multilingual_greeting_detection():
    for opener in ("Hallo Thomas,", "Sehr geehrte Frau Müller,", "Bonjour Marie,", "Hola Ana,"):
        assert svc._draft_has_greeting(f"{opener}\n\nText.", "Hi,"), opener


def test_multilingual_closing_detection():
    for closer in ("Mit freundlichen Grüßen", "Viele Grüße\nBaher", "Cordialement", "Un saludo"):
        assert svc._draft_has_closing(f"Text.\n\n{closer}", "Best,"), closer
