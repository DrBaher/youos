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


def test_google_system_mailboxes_hard_skip():
    """Google's machine local-parts (Gemini notes, Drive shares, calendar
    notifications) hard-skip — a baheros queue review found the agent drafting
    replies to them. They live at ``@google.com`` so the automation-domain list
    (which can't blanket a human-tended domain) misses them."""
    for email in (
        "gemini-notes@google.com",
        "drive-shares-dm-noreply@google.com",
        "calendar-notification@google.com",
        "docs-noreply@google.com",
    ):
        v = classify(_msg(sender=f"Google <{email}>", sender_email=email))
        assert not v.needs_reply, email
        assert any("google system mailbox" in r for r in v.reasons), email


def test_real_human_at_google_not_skipped():
    """A real person at ``@google.com`` must NOT be caught by the system-mailbox
    rule (it matches only machine local-parts)."""
    v = classify(
        _msg(
            sender="Jane Eng <jane@google.com>",
            sender_email="jane@google.com",
            body="Hi Baher, can you confirm the demo time on Thursday?",
        )
    )
    assert not any("google system mailbox" in r for r in v.reasons)


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
            return 12 if email == "vanessa@work.example" else 0

    marginal_body = "FYI — see the attached numbers."
    msg_known = _msg(
        sender="Vanessa <vanessa@work.example>",
        sender_email="vanessa@work.example",
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


# --- b30 regressions (14-day work QA) ----------------------------------
# QA found three false-positives in the 14-day work inbox that all came
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


def test_calendar_notification_hard_skipped_as_google_system_mailbox():
    """`calendar-notification@google.com` is now hard-skipped by the Google
    system-mailbox rule (was only a soft 'operational mailbox' penalty, which a
    strong body signal could override). Strict improvement — this is machine
    mail that must never get a draft."""
    v = classify(
        _msg(
            sender="Google Calendar <calendar-notification@google.com>",
            sender_email="calendar-notification@google.com",
            body="You have no events scheduled today.",
        )
    )
    assert not v.needs_reply
    assert any("google system mailbox" in r for r in v.reasons)


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

    body = "Money received from Work AI FlexCo. Please review."
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
    assert any("suppressed (template/recap)" in r for r in v.reasons)
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


# --- VIP routing (audit Tier 2) -------------------------------------------


def test_vip_sender_gets_boost_and_flag():
    # A bland message that would normally sit near the boundary.
    v = classify(_msg(body="FYI, see below.", subject="update"), vip_senders=["@partner.com"])
    assert v.vip is True
    assert any("VIP" in r for r in v.reasons)
    assert v.needs_reply  # +0.25 boost lifts it over the 0.6 threshold


def test_vip_match_by_exact_email():
    v = classify(_msg(sender_email="cofounder@startup.dev"), vip_senders=["cofounder@startup.dev"])
    assert v.vip is True


def test_non_vip_sender_not_boosted():
    v = classify(_msg(body="FYI, see below.", subject="update"), vip_senders=["@other.com"])
    assert v.vip is False
    assert not any("VIP" in r for r in v.reasons)


def test_vip_automation_still_hard_skipped():
    # A VIP domain's newsletter is still hard-skipped — the VIP boost never runs
    # because hard-skips return first.
    v = classify(
        _msg(headers={"list-unsubscribe": "<mailto:u@partner.com>"}),
        vip_senders=["@partner.com"],
    )
    assert not v.needs_reply
    assert v.vip is False


# --- b84: German/transactional false-positive (found turning the agent on) ----


def test_german_order_confirmation_not_drafted():
    """A German Amazon order confirmation ('Ordered:' subject + German body,
    even with a stray question) must not be drafted."""
    v = classify(_msg(
        sender='"Amazon.de" <bestellbestaetigung@amazon.de>',
        sender_email="bestellbestaetigung@amazon.de",
        subject="Ordered: 'Tracks Junior Mirror Goggle'",
        body="Hallo, vielen Dank für Ihre Bestellung (Bestellbestätigung). Brauchen Sie Hilfe?",
    ))
    assert not v.needs_reply, f"score={v.score} reasons={v.reasons}"
    assert any("transactional" in r for r in v.reasons)


def test_transactional_suppresses_false_history_boost():
    """A transactional template must not get the +0.20 prior-history boost from
    a corpus-noise reply pair (an ingested confirmation)."""
    class FakeHistory:
        def count_for(self, _e):
            return 5

    v = classify(
        _msg(subject="Order confirmation", body="Your order has been placed. Anything else?"),
        history=FakeHistory(),
    )
    assert any("transactional" in r for r in v.reasons)
    assert not any("prior history (5 reply pairs)" in r for r in v.reasons)


def test_real_question_to_person_still_drafts():
    """Guard: the transactional terms don't over-suppress a genuine human ask."""
    v = classify(_msg(body="Could you confirm the delivery address for the order I placed? Thanks."))
    assert v.needs_reply


# --- b205: addressed-to-me, marketing/list, meeting summaries ---------------

ME = ["you@example.com"]


def test_addressed_directly_in_to_is_not_penalized():
    v = classify(_msg(
        body="Hi — could you confirm the timeline for next week?",
        headers={"to": "You <you@example.com>", "cc": "Bob <bob@x.com>"},
    ), account_emails=ME)
    assert v.needs_reply is True
    assert not any("recipient" in r for r in v.reasons)


def test_cc_only_is_demoted():
    v = classify(_msg(
        body="Could you confirm the timeline for next week?",
        headers={"to": "Colleague <colleague@x.com>", "cc": "You <you@example.com>"},
    ), account_emails=ME)
    assert any("CC'd" in r for r in v.reasons)
    assert v.needs_reply is False        # demoted below threshold
    assert v.surface_for_review is True  # but still surfaced, not buried


def test_not_a_recipient_strongly_demoted():
    v = classify(_msg(
        body="Could you confirm the timeline for next week?",
        headers={"to": "Colleague <colleague@x.com>", "cc": "Other <other@x.com>"},
    ), account_emails=ME)
    assert any("via alias" in r for r in v.reasons)
    assert v.needs_reply is False


def test_addressed_to_me_noop_without_account_emails():
    # Back-compat: no account_emails → no recipient penalty applied.
    v = classify(_msg(
        headers={"to": "Colleague <colleague@x.com>"},
    ))
    assert not any("recipient" in r for r in v.reasons)


def test_addressed_to_me_noop_without_parseable_recipients():
    v = classify(_msg(headers={}), account_emails=ME)
    assert not any("recipient" in r for r in v.reasons)


def test_list_id_header_hard_skips():
    v = classify(_msg(headers={"list-id": "<news.acme.com>"}), account_emails=ME)
    assert v.needs_reply is False
    assert v.score == 0.0
    assert "list-id" in v.reasons[0]


def test_precedence_bulk_hard_skips():
    v = classify(_msg(headers={"precedence": "bulk"}))
    assert v.needs_reply is False
    assert v.score == 0.0


def test_meeting_summary_subject_is_demoted():
    v = classify(_msg(
        subject="Meeting summary — Q3 planning sync",
        body="Here are the notes from our meeting. Action items: review the deck, confirm owners.",
    ))
    assert any("meeting recap/summary" in r for r in v.reasons)
    assert v.needs_reply is False


def test_marketing_body_footer_penalty():
    v = classify(_msg(
        sender="Acme <hello@acme.com>", sender_email="hello@acme.com",
        subject="Big news from Acme",
        body="Check out our new product! You're receiving this email because you signed up. Unsubscribe here.",
    ))
    assert any("marketing/bulk body footer" in r for r in v.reasons)


# --- b207: calendar invites + invite responses -------------------------------

def test_calendar_invitation_subject_hard_skips():
    v = classify(_msg(subject="Invitation: Q3 planning sync @ Thu Jun 5, 2pm",
                       body="When: Thursday. Where: Zoom. Guests: ..."))
    assert v.needs_reply is False
    assert v.score == 0.0
    assert "calendar" in v.reasons[0]


def test_calendar_invite_responses_hard_skip():
    for subj in (
        "Accepted: Q3 planning sync @ Thu",
        "Declined: Q3 planning sync @ Thu",
        "Tentative: Q3 planning sync @ Thu",
        "Canceled event: Q3 planning sync",
        "Updated invitation: Q3 planning sync @ Fri",
        "Re: Invitation: Q3 planning sync @ Thu",
    ):
        v = classify(_msg(subject=subj, body="calendar details"))
        assert v.needs_reply is False, subj
        assert v.score == 0.0, subj


def test_text_calendar_content_type_hard_skips():
    v = classify(_msg(
        subject="Meeting",
        headers={"content-type": 'text/calendar; method=REQUEST; charset="UTF-8"'},
        body="BEGIN:VCALENDAR ...",
    ))
    assert v.needs_reply is False
    assert v.score == 0.0


def test_normal_reply_not_mistaken_for_calendar():
    # A normal threaded reply must NOT be calendar-skipped.
    v = classify(_msg(
        subject="Re: Q3 planning — could you confirm the budget?",
        body="Could you confirm the Q3 budget by Friday?",
    ))
    assert not any("calendar" in r for r in v.reasons)
    assert v.needs_reply is True


# --- b214: self-sent + more automation domains -------------------------------

def test_from_own_address_hard_skips():
    v = classify(_msg(sender="You <you@example.com>", sender_email="you@example.com",
                      subject="Pay Drei", body="note to self"),
                 account_emails=["you@example.com"])
    assert v.needs_reply is False
    assert v.score == 0.0
    assert "your own address" in v.reasons[0]


def test_from_own_address_noop_without_account_emails():
    # Without knowing the user's addresses, can't detect self-sent (no crash).
    v = classify(_msg(sender_email="you@example.com"))
    assert not any("your own address" in r for r in v.reasons)


def test_docusign_subdomain_hard_skips():
    v = classify(_msg(sender="DocuSign <dse@eumail.docusign.net>",
                      sender_email="dse@eumail.docusign.net",
                      subject="Completed: license agreement", body="All parties have completed."))
    assert v.needs_reply is False and v.score == 0.0


def test_booking_subdomain_hard_skips():
    v = classify(_msg(sender="Booking.com <x@property.booking.com>",
                      sender_email="x@property.booking.com",
                      subject="Direct check-in", body="Your stay details."))
    assert v.needs_reply is False and v.score == 0.0


# --- b215: standards-based automated-mail headers (sender-agnostic) ----------

def test_auto_submitted_header_hard_skips():
    for val in ("auto-generated", "auto-replied"):
        v = classify(_msg(headers={"auto-submitted": val},
                          subject="Your receipt", body="Thanks for your order."))
        assert v.needs_reply is False and v.score == 0.0, val
        assert "auto-submitted" in v.reasons[0]


def test_auto_submitted_no_is_not_skipped():
    # RFC 3834: "no" means NOT automated → must not be skipped.
    v = classify(_msg(headers={"auto-submitted": "no"},
                      body="Could you confirm the timeline for next week?"))
    assert not any("auto-submitted" in r for r in v.reasons)
    assert v.needs_reply is True


def test_x_auto_response_suppress_hard_skips():
    v = classify(_msg(headers={"x-auto-response-suppress": "All"},
                      subject="System notification", body="Automated message."))
    assert v.needs_reply is False and v.score == 0.0


def test_precedence_auto_reply_hard_skips():
    v = classify(_msg(headers={"precedence": "auto_reply"}, body="I am out of office."))
    assert v.needs_reply is False and v.score == 0.0


# --- b219: group / team thread demotion --------------------------------------

ME_ORG = ["baher@work.example", "you@gmail.com"]


def _to(*emails):
    return ", ".join(f"X <{e}>" for e in emails)


def test_in_to_among_many_still_drafts():
    # Policy: the group/Cc demotion applies ONLY when you're not addressed
    # directly. You're in To (even alongside 4+ others) → it's for you → draft.
    v = classify(_msg(
        body="Could you confirm the plan? please review.",
        headers={"to": _to("baher@work.example", "a@x.com", "b@x.com", "c@x.com", "d@x.com")},
    ), account_emails=ME_ORG)
    assert v.needs_reply is True
    assert not any("group thread" in r for r in v.reasons)


def test_group_thread_demotes_only_when_not_in_to():
    # You're NOT in To (Cc-only) on a 5+ thread → a To recipient owns it → demote.
    v = classify(_msg(
        body="Could you confirm the plan? please review.",
        headers={
            "to": _to("a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"),
            "cc": _to("baher@work.example"),
        },
    ), account_emails=ME_ORG)
    assert v.needs_reply is False
    assert v.surface_for_review is True
    assert any("group thread" in r for r in v.reasons)


def test_small_thread_with_colleague_still_drafts():
    # b220: a small direct thread (you + a colleague + an external — e.g. a
    # 3-person intro) must STILL draft. Being directly addressed on a small
    # thread means it's for you, even if a colleague is copied.
    v = classify(_msg(
        body="Could you confirm the plan for next week?",
        headers={"to": _to("baher@work.example", "colleague@work.example", "ext@partner.com")},
    ), account_emails=ME_ORG)
    assert v.needs_reply is True
    assert not any("group thread" in r or "team thread" in r for r in v.reasons)


def test_direct_small_thread_still_drafts():
    # Addressed to you + one external person, no colleague → still drafts.
    v = classify(_msg(
        body="Could you confirm the timeline for next week?",
        headers={"to": _to("baher@work.example", "client@partner.com")},
    ), account_emails=ME_ORG)
    assert v.needs_reply is True
    assert not any("group thread" in r or "team thread" in r for r in v.reasons)


def test_sole_to_recipient_drafts_despite_many_cc():
    # The reported case: you are the SOLE To recipient; the others are all Cc.
    # Being directly addressed overrides the Cc count — must draft, not surface.
    v = classify(_msg(
        body="Hi Baher, thank you so much for the follow-up. Could you confirm?",
        headers={
            "to": _to("baher@work.example"),
            "cc": _to("a@x.com", "b@x.com", "c@x.com", "d@x.com"),
        },
    ), account_emails=ME_ORG)
    assert v.needs_reply is True
    assert not any("group thread" in r for r in v.reasons)


def test_cc_only_large_thread_still_demotes():
    # You're not in To at all (Cc-only) on a big thread → a To recipient owns it,
    # so the group demotion still applies.
    v = classify(_msg(
        body="Could you confirm the plan? please review.",
        headers={
            "to": _to("boss@work.example", "a@x.com", "b@x.com", "c@x.com"),
            "cc": _to("baher@work.example"),
        },
    ), account_emails=ME_ORG)
    assert any("group thread" in r for r in v.reasons)


def test_public_domain_not_treated_as_colleague():
    # A gmail.com recipient must NOT count as a colleague (you uses gmail).
    v = classify(_msg(
        body="Could you confirm the timeline for next week?",
        headers={"to": _to("baher@work.example", "someone@gmail.com", "other@gmail.com")},
    ), account_emails=ME_ORG)
    # 3 total recipients (< 5) and no org colleague → not demoted as team thread.
    assert v.needs_reply is True


# --- bulk/marketing draft-tier cap (b230) ------------------------------------


def test_marketing_footer_caps_at_surface_tier():
    """Bulk mail with strong bonus signals (question, short body, imperative)
    used to clear the draft threshold despite the -0.20 footer penalty — and
    the model tends to continue the sender's pitch. Bulk now surfaces, never
    drafts."""
    v = classify(
        _msg(
            sender="Sydney Castle <sydney@levelvector.example>",
            sender_email="sydney@levelvector.example",
            subject="A couple of ways of seeing it",
            body=(
                "We combined balances into one LOC, up to a single limit. "
                "If that falls to someone else on your team, who would I reach? "
                "Please review and unsubscribe here if you prefer."
            ),
        ),
        threshold=0.6,
    )
    assert not v.needs_reply
    assert v.surface_for_review
    assert any("marketing/bulk body footer" in r for r in v.reasons)
    assert any("surfaced, not drafted" in r for r in v.reasons)


def test_marketing_footer_vip_still_drafts():
    v = classify(
        _msg(
            sender="Important Person <vip@partner.com>",
            sender_email="vip@partner.com",
            body=(
                "Could you confirm the timeline for next week? "
                "You can unsubscribe from these updates any time."
            ),
        ),
        threshold=0.6,
        vip_senders=["vip@partner.com"],
    )
    assert v.needs_reply


def test_marketing_footer_below_threshold_still_surfaces():
    v = classify(
        _msg(
            sender="News Bot <news@blast.example>",
            sender_email="news@blast.example",
            body="Our latest roundup. View this email in your browser.",
        ),
        threshold=0.6,
    )
    assert not v.needs_reply
    assert v.surface_for_review


def test_noreply_never_drafts_even_when_addressed_and_actionable():
    """A noreply sender with strong positive signals (question + imperative +
    short body) used to clear threshold and DRAFT. You can't reply to noreply →
    it must surface, never draft. Live 867-mail eval: Uber/AWS/Azure dunning."""
    v = classify(
        _msg(
            sender="Microsoft Azure <azure-noreply@microsoft.com>",
            sender_email="azure-noreply@microsoft.com",
            headers={"to": "me@example.com"},
            subject="Your payment is past due—pay now to continue service?",
            body="Please update your billing info and pay now. Can you confirm?",
        ),
        account_emails=["me@example.com"],
    )
    assert not v.needs_reply, v.reasons
    assert v.surface_for_review
    assert any("can't reply" in r or "surfaced, not drafted" in r for r in v.reasons), v.reasons


def test_transactional_automation_from_operational_mailbox_never_drafts():
    """A billing/receipt template from an operational mailbox is automation —
    surface, don't draft (even addressed-to-me with bonuses)."""
    v = classify(
        _msg(
            sender="Amazon Web Services <invoicing@amazonaws.com>",
            sender_email="invoicing@amazonaws.com",
            headers={"to": "me@example.com"},
            subject="Your AWS invoice / billing statement is available",
            body="Your invoice is ready. Please review your billing statement now.",
        ),
        account_emails=["me@example.com"],
    )
    assert not v.needs_reply, v.reasons


def test_human_with_same_signals_still_drafts():
    """Guard: a HUMAN (not noreply/operational) with question+imperative still
    drafts — the never-draft rule is tied to automation senders, not the signals
    that were demoting the noreply/billing automation above."""
    v = classify(
        _msg(
            sender="Nadine <nadine@medicus.ai>",
            sender_email="nadine@medicus.ai",
            headers={"to": "me@example.com"},
            subject="Quick question",
            body="Can you confirm the budget before I send it? Thanks!",
        ),
        account_emails=["me@example.com"],
    )
    assert v.needs_reply, v.reasons


def test_vip_noreply_override_still_drafts():
    """A VIP overrides the noreply demote (consistent with group/bulk)."""
    v = classify(
        _msg(
            sender="Important <vip-noreply@partner.com>",
            sender_email="vip-noreply@partner.com",
            headers={"to": "me@example.com"},
            subject="Action needed?",
            body="Please review and confirm. Can you reply today?",
        ),
        account_emails=["me@example.com"],
        vip_senders=["vip-noreply@partner.com"],
    )
    assert v.needs_reply, v.reasons


class _FakeHistory:
    """Stub SenderHistory: a fixed prior-reply count for any sender."""
    def __init__(self, n): self._n = n
    def count_for(self, sender_email): return self._n


_COLD_PITCH = dict(
    sender="Zsófia Fazekas <fazekas@kylla.co.uk>",
    sender_email="fazekas@kylla.co.uk",
    headers={"to": "me@example.com"},
    subject="Investment Capital Participation",
    body="Dear Baher, I understand you are looking to raise capital for Medicus AI. "
         "We might be interested in participating in this round. Please send me your pitch deck "
         "and your availability so we can identify if this could be a good fit for our firm.",
)


def test_confident_cold_pitch_first_contact_surfaces_not_drafts():
    v = classify(_msg(**_COLD_PITCH), account_emails=["me@example.com"])  # history=None → first contact
    assert not v.needs_reply, v.reasons
    assert v.surface_for_review
    assert any("cold pitch" in r for r in v.reasons), v.reasons


def test_cold_pitch_from_established_sender_still_drafts():
    """Prior reply history = an established correspondent → draft even if the
    message reads pitch-like (the demote is first-contact only)."""
    v = classify(_msg(**_COLD_PITCH), history=_FakeHistory(4), account_emails=["me@example.com"])
    assert v.needs_reply, v.reasons


def test_genuine_first_contact_without_pitch_signals_still_drafts():
    v = classify(
        _msg(
            sender="Dana Roy <dana@hospital.org>",
            sender_email="dana@hospital.org",
            headers={"to": "me@example.com"},
            subject="Following up from the conference",
            body="Hi Baher, great meeting you last week. Could you confirm the timeline "
                 "for the integration so I can brief my team? Thanks!",
        ),
        account_emails=["me@example.com"],
    )
    assert v.needs_reply, v.reasons
