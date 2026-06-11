"""b257: the b254 ASCII-tokenizer fix extended across the text pipeline.

The retrieval tokenizer (b254) was one instance of an ASCII-only `[a-z]`
pattern in a product that advertises multilingual drafting. This sweep fixed
the other load-bearing ones: the voice metric (feeds the golden composite /
adapter-promotion gate and model comparison), sender-profile topic
extraction, and project-fact content-word matching.
"""

from __future__ import annotations

from app.evaluation.voice_match import _tokens
from app.generation.service import _extract_content_words
from scripts.build_sender_profiles import extract_topics


def test_voice_tokens_capture_non_english_and_keep_contractions():
    assert _tokens("don't it's") == ["don't", "it's"]  # contractions intact
    assert _tokens("Frühstück können wir") == ["frühstück", "können", "wir"]
    ar = _tokens("مرحبا كيف الحال")
    assert len(ar) == 3  # previously: []  → lexical overlap was blind
    assert _tokens("naïve café") == ["naïve", "café"]


def test_voice_lexical_overlap_nonzero_for_matching_non_english():
    from app.evaluation.voice_match import _lexical_overlap

    # Identical Arabic draft/reference must score high, not ~0.
    text = "شكرا لك سأراجع العرض وأعود إليك غدا"
    assert _lexical_overlap(text, text) > 0.9


def test_topic_extraction_handles_non_english_subjects():
    topics = extract_topics(["Vertrag Verlängerung", "Vertrag Frage", "Frühstück Termin"])
    assert "vertrag" in topics  # previously: ASCII [a-z] → garbage/empty
    ar = extract_topics(["بخصوص العرض النهائي", "العرض والسعر"])
    assert any("العرض" == t for t in ar)


def test_content_words_match_non_english():
    words = _extract_content_words("بخصوص العرض النهائي للمشروع")
    assert len(words) >= 2  # previously: [] → project facts never matched
    # English path unchanged
    assert "project" in _extract_content_words("the project deadline review")


def test_english_voice_tokens_unchanged():
    assert _tokens("Re: the Q2 invoice, I'll review it") == [
        "re", "the", "q2", "invoice", "i'll", "review", "it",
    ]
