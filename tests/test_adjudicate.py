"""Tests for borderline LLM adjudication (Phase A2).

The warm model gives a one-word PERSONAL/BROADCAST verdict that can VETO a
borderline draft. These tests inject a fake ``complete_fn`` so no model server
is needed.
"""

from __future__ import annotations

from app.agent.adjudicate import AdjudicationResult, _parse, adjudicate

# --- _parse ----------------------------------------------------------------


def test_parse_one_word_answers():
    assert _parse("BROADCAST").is_broadcast
    assert _parse("PERSONAL").is_broadcast is False
    assert _parse("broadcast\n").verdict == "broadcast"
    assert _parse("personal.").verdict == "personal"


def test_parse_leading_word_then_explanation():
    assert _parse("Broadcast — this is a marketing newsletter.").is_broadcast
    assert _parse("Personal: a colleague asking a question.").verdict == "personal"


def test_parse_mentions_class_when_not_leading():
    assert _parse("This looks like an automated notification.").is_broadcast
    assert _parse("Seems like a personal note to me.").verdict == "personal"


def test_parse_ambiguous_or_empty_is_unknown_no_veto():
    assert _parse("").verdict == "unknown"
    assert _parse("not sure").verdict == "unknown"
    # Mentions both classes → can't decide → no veto.
    assert _parse("could be personal or a broadcast").is_broadcast is False


# --- adjudicate ------------------------------------------------------------


def test_adjudicate_returns_broadcast_verdict():
    res = adjudicate(
        subject="Your weekly digest",
        sender="news@digest.com",
        body="Here are this week's top stories ...",
        complete_fn=lambda p: "BROADCAST",
    )
    assert isinstance(res, AdjudicationResult)
    assert res.is_broadcast


def test_adjudicate_returns_personal_verdict():
    res = adjudicate(
        subject="Quick question on the Q3 numbers",
        sender="alice@partner.com",
        body="Could you confirm the pricing?",
        complete_fn=lambda p: "PERSONAL",
    )
    assert res is not None
    assert res.is_broadcast is False


def test_adjudicate_failure_is_isolated_returns_none():
    """A model error must not raise — it returns None so the caller keeps the
    heuristic verdict (None means 'no veto')."""
    def _boom(_p):
        raise RuntimeError("model down")

    res = adjudicate(subject="s", sender="x@y.com", body="b", complete_fn=_boom)
    assert res is None


def test_adjudicate_unparseable_answer_does_not_veto():
    """A model answer we can't read confidently must not veto."""
    res = adjudicate(subject="s", sender="x@y.com", body="b", complete_fn=lambda p: "hmm, dunno")
    assert res is not None
    assert res.is_broadcast is False
    assert res.verdict == "unknown"


def test_adjudicate_prompt_includes_message_fields():
    seen = {}

    def _capture(p):
        seen["prompt"] = p
        return "PERSONAL"

    adjudicate(
        subject="Lunch Thursday?",
        sender="bob@friend.com",
        body="Are you free Thursday at noon?",
        complete_fn=_capture,
    )
    p = seen["prompt"]
    assert "Lunch Thursday?" in p
    assert "bob@friend.com" in p
    assert "Thursday at noon" in p
    assert "PERSONAL or BROADCAST" in p
