"""Cold-outreach detection (QA fix #3 of 3).

The LoRA politely accepts pushy outbounds. The detector catches the *inbound*
shape so generation can nudge the prompt toward a polite decline. These pin
the heuristic so the Jess QA case still fires and a legitimate work email
asking for a quick call doesn't.
"""

from __future__ import annotations

from app.core.cold_outreach import (
    COLD_OUTBOUND_THRESHOLD,
    DECLINE_NUDGE,
    detect_cold_outbound,
)


def test_jess_qa_case_classified_as_cold_outbound():
    """The exact QA case that motivated this — every signal we want to catch."""
    v = detect_cold_outbound(
        subject="Boost your YouOS launch — 30-min call?",
        body=(
            "Hi there, I work with Apple-Silicon SaaS founders to 10x their launch "
            "traction. Saw YouOS on Twitter — really cool product. Can I steal 30 min "
            "next week to share what's worked for our other portfolio founders?"
        ),
        sender_email="jess.martin@acmemarketing.io",
    )
    assert v.is_cold
    assert v.score >= COLD_OUTBOUND_THRESHOLD
    # Receipt of evidence — at least four classes of signal fired.
    assert any(h.startswith("subject:") for h in v.hits)
    assert any(h.startswith("body:") for h in v.hits)
    assert any(h.startswith("domain:") for h in v.hits)


def test_legitimate_partner_pricing_inquiry_is_NOT_cold_outbound():
    """The Alex/Stripe case from QA — genuine business inquiry, not pushy."""
    v = detect_cold_outbound(
        subject="Quick question — pricing for next quarter",
        body=(
            "Hi Baher, We're planning Q3 budget and I wanted to check if you're still "
            "happy with current pricing, or whether we should look at moving you to the "
            "enterprise tier. Could you share a rough sense of monthly volume?"
        ),
        sender_email="alex.chen@stripe.com",
    )
    assert not v.is_cold, f"false positive: {v}"


def test_friend_casual_message_is_NOT_cold_outbound():
    v = detect_cold_outbound(
        subject="Sat dinner?",
        body="yo — free saturday? new ramen place opened near mine. 7pm? bring a bottle",
        sender_email="sam@gmail.com",
    )
    assert not v.is_cold


def test_internal_team_quick_question_is_NOT_cold_outbound():
    """An internal teammate asking for a quick chat shouldn't trip the heuristic."""
    v = detect_cold_outbound(
        subject="quick chat about the API spec?",
        body="Hey — got 10 min later today to walk through the new endpoints?",
        sender_email="vanessa@work.example",
    )
    assert not v.is_cold


def test_high_confidence_body_pattern_counts_double():
    """'I work with [type] founders' is the smoking gun — weighted 2× so the
    detector still fires on outreach with light subject signal."""
    v = detect_cold_outbound(
        subject="Reaching out",
        body="I work with Apple-Silicon SaaS founders to scale their growth. Open to a quick call?",
        sender_email="contact@unknown.io",
    )
    assert v.is_cold


def test_decline_nudge_constant_exists():
    """The generation pipeline imports DECLINE_NUDGE; pin that it's a usable
    non-empty string with the right intent."""
    assert isinstance(DECLINE_NUDGE, str)
    assert len(DECLINE_NUDGE) > 20
    assert "decline" in DECLINE_NUDGE.lower() or "clarifying" in DECLINE_NUDGE.lower()


# --- 2026-06 live-corpus cold classes (investor/M&A, tracking, opt-out) ------

def _v(subject="", body="", sender_email="x@y.com"):
    from app.core.cold_outreach import detect_cold_outbound
    return detect_cold_outbound(subject=subject, body=body, sender_email=sender_email)


def test_investor_capital_pitch_is_confident():
    v = _v(subject="Investment Capital Participation",
           body="I understand you are looking to raise capital for Medicus AI. "
                "We might be interested in participating in this round. Send me your pitch deck.")
    assert v.is_cold and v.confident, v.hits


def test_german_ma_intro_is_confident():
    v = _v(subject="Strategisches Interesse - Austausch",
           body="Ich möchte mich Ihnen kurz vorstellen. Ich verantworte bei TH Global Capital "
                "den DACH-Markt, eine Investmentbank mit Fokus auf M&A und Kapitalbeschaffung.")
    assert v.is_cold and v.confident, v.hits


def test_sales_engagement_tracking_link_is_confident():
    v = _v(subject="Customers, talent, capital",
           body="Every growth-stage founder I talk to is closing the next round. "
                "https://d5nLkf04.na1.hs-sales-engage.com/Ctc/JA+23284/abc")
    assert v.is_cold and v.confident, v.hits


def test_individual_unsub_savings_pitch_is_confident():
    v = _v(subject="Reflect meet up",
           body="I noticed your team runs ChatGPT at full price. We cut AI costs by 30-50%. "
                'Not for you? Just reply "unsub" and I will never bug you again.')
    assert v.is_cold and v.confident, v.hits


def test_genuine_first_email_is_not_confident():
    """A real first-contact business email (no investor/tracking/opt-out cues)
    must NOT be flagged confident-cold — it should still draft."""
    v = _v(subject="Following up from the conference",
           body="Hi Baher, great meeting you last week. Could you confirm the timeline "
                "for the integration so I can brief my team? Thanks!")
    assert not v.confident, v.hits
