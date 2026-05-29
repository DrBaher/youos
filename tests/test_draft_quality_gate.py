"""Tests for the per-draft quality gate (Phase A1).

``draft_quality_score`` and ``_is_generic_ack`` decide whether a generated
draft is good enough to *act* on (auto-push / later auto-send) — gating on the
draft's own quality, not just the needs-reply score.
"""

from __future__ import annotations

from app.generation.service import _is_generic_ack, draft_quality_score
from app.retrieval.service import RetrievalMatch


def _reply(reply: str, score: float = 5.0) -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="gmail",
        source_id="1",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        inbound_text="inbound",
        reply_text=reply,
        snippet="inbound",
    )


# --- _is_generic_ack -------------------------------------------------------


def test_is_generic_ack_flags_contentless_acknowledgements():
    assert _is_generic_ack("Thanks for the update!")
    assert _is_generic_ack("Got it, thanks.")
    assert _is_generic_ack("Sounds good, thanks!")
    assert _is_generic_ack("Will do.")


def test_is_generic_ack_ignores_substantive_replies():
    # Has a concrete commitment / content.
    assert not _is_generic_ack(
        "Thanks for the update — I've pushed the contract back to Thursday "
        "and looped in legal so they can review the indemnity clause."
    )
    # A question is real engagement, not a contentless ack.
    assert not _is_generic_ack("Thanks! Could you confirm the Tuesday slot?")


def test_is_generic_ack_handles_empty():
    assert not _is_generic_ack("")
    assert not _is_generic_ack("   ")


# --- draft_quality_score ---------------------------------------------------


def test_quality_score_zero_for_empty_draft():
    assert draft_quality_score("", reply_pairs=None, target_words=30) == 0.0
    assert draft_quality_score("   ", reply_pairs=None, target_words=30) == 0.0


def test_quality_score_collapses_for_generic_ack():
    """A generic acknowledgement is driven near zero even if structurally fine —
    this is what kills the live newsletter false positives."""
    q = draft_quality_score(
        "Thanks for the update, I'll check it out!",
        reply_pairs=None,
        target_words=8,
    )
    assert q <= 0.15


def test_quality_score_rewards_a_real_draft_in_the_users_voice():
    """A substantive draft that echoes the user's exemplar voice scores well
    above the default 0.5 floor."""
    refs = [
        _reply("Sure — Tuesday at 2pm works for me, see you then."),
        _reply("Yes, let's lock in Tuesday. I'll send a calendar invite shortly."),
        _reply("Tuesday works. I'll bring the updated deck along."),
    ]
    q = draft_quality_score(
        "Hi Sam — Tuesday at 2pm works for me. I'll send a calendar invite "
        "and bring the updated deck along.",
        reply_pairs=refs,
        target_words=22,
        greeting="Hi",
        closing="",
        model_used="qwen2.5-1.5b-lora",
    )
    assert 0.0 <= q <= 1.0
    assert q > 0.5, f"a strong in-voice draft should clear the floor, got {q}"


def test_quality_score_discounts_non_lora_fallback():
    """An identical draft scores lower when produced by a cloud/base fallback
    than by the user's LoRA — it's less likely to be in-voice."""
    refs = [_reply("Sure, Tuesday at 2pm works."), _reply("Yes, Tuesday is good.")]
    draft = "Hi Sam — Tuesday at 2pm works for me, see you then."
    q_lora = draft_quality_score(
        draft, reply_pairs=refs, target_words=12, model_used="qwen2.5-1.5b-lora"
    )
    q_cloud = draft_quality_score(
        draft, reply_pairs=refs, target_words=12, model_used="claude-cloud"
    )
    assert q_cloud < q_lora


def test_quality_score_discounts_empty_output_retry():
    refs = [_reply("Sure, Tuesday at 2pm works.")]
    draft = "Hi Sam — Tuesday at 2pm works for me, see you then."
    q_clean = draft_quality_score(
        draft, reply_pairs=refs, target_words=12, model_used="qwen2.5-1.5b-lora"
    )
    q_retried = draft_quality_score(
        draft, reply_pairs=refs, target_words=12, model_used="qwen2.5-1.5b-lora",
        empty_output_retried=True,
    )
    assert q_retried < q_clean


def test_quality_score_without_exemplars_uses_structure_only():
    """No exemplars to compare against → score still produced from structure
    alone (never crashes, stays in range)."""
    q = draft_quality_score(
        "Hi Sam — Tuesday at 2pm works for me. I'll send an invite shortly.",
        reply_pairs=None,
        target_words=15,
        greeting="Hi",
    )
    assert 0.0 <= q <= 1.0
