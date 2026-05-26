"""Voice-match metric — does a draft sound like the user's real reply.

These pin the behaviour the cross-model comparison depends on: identical text
scores ~1, unrelated text scores low, the optional semantic component is used
when an embedder is injected (and ignored gracefully when it fails), and the
score is wired into evaluate_case only when a reference reply is present.
"""

from __future__ import annotations

from app.evaluation.voice_match import voice_match_score


def test_identical_text_scores_near_one():
    ref = "Hi Sam,\n\nThanks for the note. I can do Thursday at 2pm — does that work?\n\nBest,\nAlex"
    s = voice_match_score(ref, ref)
    assert s["voice_match"] >= 0.99
    assert s["lexical_overlap"] == 1.0
    assert s["length_ratio"] == 1.0
    assert s["style_similarity"] == 1.0
    assert s["greeting_match"] is True
    assert s["closing_match"] is True
    assert s["semantic_similarity"] is None  # no embedder injected


def test_unrelated_text_scores_low():
    ref = "Hi Sam, Thursday at 2pm works for me. Best, Alex"
    draft = "ATTENTION: Your account has been suspended. Click here immediately to verify."
    s = voice_match_score(draft, ref)
    assert s["voice_match"] < 0.5


def test_closer_style_scores_higher():
    ref = "Hey Jordan, sounds good — let's lock it in for Friday. Cheers, Pat"
    close = "Hey Jordan, works for me — let's do Friday. Cheers, Pat"
    far = "Dear Mr. Jordan, I am writing to formally confirm our appointment scheduled for the upcoming Friday. Sincerely."
    assert voice_match_score(close, ref)["voice_match"] > voice_match_score(far, ref)["voice_match"]


def test_empty_strings_do_not_crash():
    assert voice_match_score("", "")["voice_match"] >= 0.0  # both empty → defined, no crash
    assert voice_match_score("", "Hi there")["voice_match"] == 0.0
    assert voice_match_score("Hi there", "")["voice_match"] == 0.0


def test_semantic_component_used_when_embedder_injected():
    # Fake embedder: identical vectors → cosine 1.0, so semantic lifts the score
    # above the deterministic-only blend for a paraphrase.
    def embed(_text: str):
        return (1.0, 0.0, 0.0)

    def cosine(_a, _b):
        return 1.0

    ref = "Thanks, that works for me."
    draft = "Sounds great, see you then."
    with_sem = voice_match_score(draft, ref, embed_fn=embed, cosine_fn=cosine)
    without_sem = voice_match_score(draft, ref)
    assert with_sem["semantic_similarity"] == 1.0
    assert with_sem["voice_match"] > without_sem["voice_match"]


def test_semantic_failure_degrades_gracefully():
    def bad_embed(_text: str):
        raise RuntimeError("model not loaded")

    s = voice_match_score("a draft", "a reference", embed_fn=bad_embed)
    # Falls back to deterministic components rather than raising.
    assert s["semantic_similarity"] is None
    assert 0.0 <= s["voice_match"] <= 1.0


def test_evaluate_case_adds_voice_match_only_with_reference():
    from app.evaluation.service import evaluate_case

    case = {
        "case_key": "c1",
        "category": "work",
        "prompt_text": "Can we meet Thursday?",
        "expected_properties": {"should_contain_keywords": [], "max_words": 100, "mode": "work"},
    }
    draft = "Sure, Thursday works for me."

    without_ref = evaluate_case(case, draft, "work", "high", 2)
    assert "voice_match" not in without_ref.scores

    with_ref = evaluate_case(case, draft, "work", "high", 2, reference_reply="Yes, Thursday is great.")
    assert "voice_match" in with_ref.scores
    assert 0.0 <= with_ref.scores["voice_match"]["voice_match"] <= 1.0
    # Voice-match must not change the rule-based verdict.
    assert with_ref.pass_fail == without_ref.pass_fail
