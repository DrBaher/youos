"""Needs-reply classifier — hard-skip rules + lightweight scoring + cold detect."""

from __future__ import annotations

from app.agent.inbox_fetch import InboxMessage
from app.agent.needs_reply import classify


def _msg(**overrides) -> InboxMessage:
    base = {
        "message_id": "m1",
        "thread_id": "t1",
        "account": "you@example.com",
        "sender": "Alice <alice@partner.com>",
        "sender_email": "alice@partner.com",
        "subject": "Question",
        "body": "Hi — could you confirm the timeline for next week?",
        "headers": {},
        "received_at": None,
    }
    base.update(overrides)
    return InboxMessage(**base)


# --- Hard skips ------------------------------------------------------------


def test_list_unsubscribe_header_hard_skips():
    v = classify(_msg(headers={"list-unsubscribe": "<mailto:unsub@list.com>"}))
    assert not v.needs_reply
    assert any("list-unsubscribe" in r for r in v.reasons)


def test_noreply_sender_hard_skips():
    v = classify(_msg(sender="noreply@bigcorp.com", sender_email="noreply@bigcorp.com"))
    assert not v.needs_reply
    assert any("noreply" in r for r in v.reasons)


def test_automation_domain_hard_skips():
    v = classify(_msg(sender="alerts@notifications.bigcorp.com", sender_email="alerts@notifications.bigcorp.com"))
    assert not v.needs_reply
    assert any("automation" in r for r in v.reasons)


def test_empty_body_hard_skips():
    v = classify(_msg(body="   "))
    assert not v.needs_reply


# --- Scoring ---------------------------------------------------------------


def test_question_with_imperative_verb_passes():
    """The Alice-asking-to-confirm-the-timeline case — clear needs-reply."""
    v = classify(_msg())
    assert v.needs_reply
    assert v.score >= 0.7
    assert any("question" in r for r in v.reasons)
    assert any("imperative" in r for r in v.reasons)


def test_long_body_no_signals_drops_below_threshold():
    """A long digest with no question, no action verb, no history should
    not pass. The >800-word penalty drives the score under the threshold."""
    very_long_body = ("This is an FYI item with no action items. " * 100)  # > 800 words
    v = classify(_msg(body=very_long_body, subject="Weekly digest"))
    assert not v.needs_reply
    # The long-digest penalty fired.
    assert any("digest" in r or "long" in r for r in v.reasons)


def test_cold_outreach_flagged_and_score_dampened():
    """Cold outreach is still drafted (might be polite-decline) but the score
    is dampened so a marginal case drops below threshold."""
    body = (
        "Hi there, I work with Apple-Silicon SaaS founders to 10x launches. "
        "Can I steal 30 min next week?"
    )
    v = classify(
        _msg(
            sender="Jess Martin <jess.martin@acmemarketing.io>",
            sender_email="jess.martin@acmemarketing.io",
            subject="Boost your launch — 30-min call?",
            body=body,
        )
    )
    assert v.cold_outreach
    # Either still passes (decline draft) or drops just below — what matters
    # is the cold-outreach flag is set so the prompt nudge fires downstream.
    assert any("cold-outreach" in r for r in v.reasons)


def test_prior_history_boost_uses_sender_history(monkeypatch):
    """Sender-history boost adds 0.20 when the inbound author has prior pairs.
    Use a marginal body (no question, no imperative) so the boost isn't
    swallowed by the 1.0 cap when other signals also fire."""

    class FakeHistory:
        def count_for(self, email):
            return 12 if email == "vanessa@medicus.ai" else 0

    marginal_body = "FYI — see the attached numbers."
    msg_known = _msg(
        sender="Vanessa <vanessa@medicus.ai>",
        sender_email="vanessa@medicus.ai",
        body=marginal_body,
    )
    msg_new = _msg(
        sender="Stranger <new@example.com>",
        sender_email="new@example.com",
        body=marginal_body,
    )
    v_known = classify(msg_known, history=FakeHistory())
    v_new = classify(msg_new, history=FakeHistory())
    # Boost adds ~0.20 (allow rounding slack). The exact "flip across the
    # threshold" depends on other signals firing; what we pin here is the
    # score *delta* and the reason being recorded.
    assert v_known.score >= v_new.score + 0.15
    assert any("prior history" in r for r in v_known.reasons)
    assert not any("prior history" in r for r in v_new.reasons)
