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


def test_mailer_daemon_hard_skips():
    """Bounces and mailer-daemon ARE definitively automation; never want a reply."""
    v = classify(_msg(sender="MAILER-DAEMON <postmaster@x.com>", sender_email="postmaster@x.com"))
    assert not v.needs_reply
    assert any("mailer-daemon" in r or "bounce" in r for r in v.reasons)


def test_noreply_is_soft_penalty_not_hard_skip():
    """Real-inbox QA: transactional ``noreply@`` carries lead-form content.
    Now a soft −0.20 penalty so strong positive signals can still surface
    a real lead, while pure ``noreply@`` newsletters stay skipped (via the
    List-Unsubscribe rule that catches them separately)."""
    v = classify(
        _msg(
            sender="System <noreply@bigcorp.com>",
            sender_email="noreply@bigcorp.com",
            body="Please confirm the meeting time for tomorrow.",  # has imperative + question-ish
        )
    )
    # Penalty applied + reason recorded.
    assert any("noreply" in r for r in v.reasons)
    # Strong positive signals can rescue it: short body + imperative.
    assert v.score >= 0.45  # 0.5 base + 0.1 short + 0.1 imperative - 0.2 noreply


def test_automation_domain_hard_skips_github():
    """GitHub notifications must hard-skip — real-inbox QA found CI mail
    sneaking past the old filter and getting bad drafts."""
    v = classify(
        _msg(
            sender="DrBaher <notifications@github.com>",
            sender_email="notifications@github.com",
            subject="Some subject without service pattern",
        )
    )
    assert not v.needs_reply
    assert any("automation" in r for r in v.reasons)


def test_service_subject_pattern_hard_skips_repo_tag():
    """``[Org/Repo]`` subject prefixes (GitHub/GitLab convention) hard-skip
    even from non-automation senders. Same family of CI/notification mail."""
    v = classify(_msg(subject="[DrBaher/youos] PR run failed: CI - main"))
    assert not v.needs_reply
    assert any("service subject" in r for r in v.reasons)


def test_operational_mailbox_prefix_penalty():
    """`billing-support@`, `support@`, `info@` etc. are usually automation
    but human-tended ones can still surface — soft −0.20 penalty."""
    v = classify(
        _msg(
            sender="Supabase Billing Team <billing-support@supabase.com>",
            sender_email="billing-support@supabase.com",
            body="A short body without question or imperative.",
        )
    )
    # Penalty + reason applied.
    assert any("operational mailbox" in r for r in v.reasons)
    # And on a body with no positive signals, the case correctly drops.
    assert not v.needs_reply


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


# --- b30 regressions (14-day medicus QA) ----------------------------------
# QA found three false-positives in the 14-day medicus inbox that all came
# down to (a) operational keywords mid-local-part, (b) automation-domain
# meeting bots not in the list, (c) prior-history boost wrongly applied to
# noreply senders.


def test_workspace_noreply_gets_a_penalty():
    """`workspace-noreply@google.com` starts with 'workspace' but the
    `\\bnoreply\\b` word boundary inside NOREPLY_LOCAL_PAT catches it after
    the hyphen. The operational-mailbox pattern no longer double-charges
    noreply variants, so the penalty lands once via the noreply path."""
    v = classify(
        _msg(
            sender="The Google Workspace Team <workspace-noreply@google.com>",
            sender_email="workspace-noreply@google.com",
            body="Your subscription requires action. Please review.",
        )
    )
    # Some kind of automation-penalty fired (currently via the noreply path).
    assert any("noreply" in r for r in v.reasons), v.reasons


def test_calendar_notification_caught_by_substring_match():
    """`calendar-notification@google.com` — same: 'notification' substring."""
    v = classify(
        _msg(
            sender="Google Calendar <calendar-notification@google.com>",
            sender_email="calendar-notification@google.com",
            body="You have no events scheduled today.",
        )
    )
    assert any("operational mailbox" in r for r in v.reasons)


def test_fireflies_meeting_bot_hard_skipped_by_domain():
    """Fireflies / Otter / Loom / Calendly / Doodle are meeting-bot services
    whose mail is always automation. Added to the automation-domain list."""
    v = classify(
        _msg(
            sender="Fred from Fireflies.ai <fred@fireflies.ai>",
            sender_email="fred@fireflies.ai",
            body="Your recording for Weekly Check-in is ready. Click here to view.",
        )
    )
    assert not v.needs_reply
    assert any("automation" in r for r in v.reasons)


def test_prior_history_boost_suppressed_for_transactional_sender():
    """Real-inbox b30: `count_for(noreply@wise.com)` returned 6 (Wise
    notifications captured by ingest), so the +0.20 history boost lifted
    a pure transactional notification past threshold. Suppress when the
    sender already matched noreply / operational-mailbox."""

    class FakeHistory:
        def count_for(self, _email):
            return 6

    body = "Money received from Medicus AI FlexCo. Please review."
    v = classify(
        _msg(
            sender="Wise <noreply@wise.com>",
            sender_email="noreply@wise.com",
            body=body,
        ),
        history=FakeHistory(),
    )
    # Reason is *recorded* so the operator can see history existed; boost is
    # *not* applied.
    assert any("suppressed" in r for r in v.reasons), v.reasons
    # And without the boost, this should fall below threshold.
    assert not v.needs_reply, f"transactional + history boost should not pass: {v}"


# --- ζ: user-configured skip-list -----------------------------------------


def test_skip_list_exact_email_match_hard_skips():
    v = classify(_msg(sender_email="alice@partner.com"),
                 skip_senders=["alice@partner.com"])
    assert not v.needs_reply
    assert any("skip-list" in r for r in v.reasons)


def test_skip_list_domain_prefix_match_hard_skips():
    """`@domain` entries skip the whole org."""
    v = classify(_msg(sender_email="someone@bigcorp.com"),
                 skip_senders=["@bigcorp.com"])
    assert not v.needs_reply
    assert any("skip-list" in r for r in v.reasons)


def test_skip_list_case_insensitive():
    v = classify(_msg(sender_email="Alice@Partner.com"),
                 skip_senders=["alice@partner.com"])
    assert not v.needs_reply


def test_skip_list_no_match_keeps_existing_behaviour():
    v = classify(_msg(), skip_senders=["someone@else.com"])
    # Original Alice case still passes (question + imperative + short body).
    assert v.needs_reply


# --- Transactional templates (b51) ----------------------------------------


def test_booking_confirmation_subject_drops_below_threshold():
    """The Ali Barber Shop QA case (b50): subject = 'Booking Confirmation
    for Ali Barber Shop', body paraphrases the appointment details. Without
    the transactional-template detector it scored 0.60 (base 0.5 + imperative
    verb 0.10) and got auto-drafted. With the detector it drops to ~0.35
    and lands in surface-for-review instead."""
    v = classify(_msg(
        sender="Ali Barber Shop <office@alibarbershop.at>",
        sender_email="office@alibarbershop.at",
        subject="Booking Confirmation for Ali Barber Shop",
        body=(
            "Dear Baher, Your appointment for CUT (wash, cut & styling) is "
            "confirmed. Date: 30/05/2026, Time: 09:00-09:30, Location: Ali "
            "Barber Shop Amerlingstr. 4, 1060 Vienna, Your Barber: Faisal "
            "Barber, Price: €38. Thank you for your Booking! Looking forward "
            "to see you soon!"
        ),
    ))
    # Subject hits TRANSACTIONAL_TEMPLATE_PAT → -0.25 penalty.
    assert not v.needs_reply
    assert "transactional template (subject)" in " ".join(v.reasons)
    # Imperative-verb bonus is suppressed (template noise, not a request).
    assert any("suppressed (transactional)" in r for r in v.reasons)
    assert v.score < 0.6
    # But still high enough to surface — user can still see it if they want.
    assert v.surface_for_review


def test_body_only_template_phrase_drops_score():
    """Subject is innocuous but the body opens with 'Your booking is
    confirmed' — body-pattern match (slightly weaker, -0.20)."""
    v = classify(_msg(
        sender="Restaurant <hello@bigrest.com>",
        sender_email="hello@bigrest.com",
        subject="See you tonight",
        body=(
            "Your reservation has been confirmed for tonight at 7pm. "
            "Looking forward to having you."
        ),
    ))
    assert "transactional template (body)" in " ".join(v.reasons)


def test_human_reply_that_mentions_booking_still_passes():
    """Edge case: real human asking about a booking. Subject and body don't
    match the template patterns, so no penalty — high score from question."""
    v = classify(_msg(
        sender="Alice <alice@partner.com>",
        sender_email="alice@partner.com",
        subject="Re: Booking",
        body="Hey — could we move our booking next week to Thursday afternoon?",
    ))
    assert v.needs_reply
    assert not any("transactional" in r for r in v.reasons)


def test_order_receipt_pattern():
    v = classify(_msg(
        sender="Amazon <auto-confirm@amazon.com>",
        sender_email="auto-confirm@amazon.com",
        subject="Your order has been placed",
        body="Hi Baher, your order has been placed. Thanks for shopping.",
    ))
    assert "transactional template (subject)" in " ".join(v.reasons)
    assert not v.needs_reply


# --- Thread-reply quoted-history handling (audit Tier 1) -------------------


def test_trivial_ack_on_thread_does_not_inherit_quoted_question():
    """A 'thanks' reply on a thread whose quoted history contains a question +
    imperative must NOT be drafted — the signals must come from the NEW
    content, not the quoted block. This was a large false-positive class."""
    body = (
        "Sounds good, thanks!\n\n"
        "On Mon, May 26, 2026 at 9:00 AM Alice <alice@partner.com> wrote:\n"
        "> Could you please confirm the Q3 pricing and send the updated numbers?\n"
        "> Would Thursday work for a call?\n"
        "> Best, Alice\n"
    )
    v = classify(_msg(body=body))
    assert not v.needs_reply, f"trivial ack should not be drafted (score={v.score}, reasons={v.reasons})"


def test_real_new_question_on_thread_still_surfaces():
    """A genuine new question in the reply (above the quoted history) still
    scores as needs-reply — we strip the quote, not the new content."""
    body = (
        "Thanks! One more thing — can you also share the onboarding timeline?\n\n"
        "On Mon, May 26, 2026 at 9:00 AM Alice <alice@partner.com> wrote:\n"
        "> Here is the pricing deck.\n"
    )
    v = classify(_msg(body=body))
    assert v.needs_reply, f"a real new question should be drafted (score={v.score}, reasons={v.reasons})"
