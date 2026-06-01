"""b178 — triage precision on transactional / automation mail.

A live draft demo over baher@medicus.ai over-kept transactional mail (a
conference ticket, an invoice, an event newsletter) and drafted replies that
just restated the email. Two gaps were behind it:

  1. ``scripts/triage_demo.py`` filtered with a hand-rolled regex (no-reply /
     List-Unsubscribe / a few automation domains) that diverged from the real
     classifier — so the demo never reflected production.
  2. The production ``needs_reply.classify`` itself missed invoices, tickets,
     and operational ``invoices@/tickets@/events@/...`` mailboxes.

These tests pin BOTH: transactional/automation samples must be needs_reply=False,
genuine reply-worthy mail (a person's meeting invite, a client question, a
payroll confirmation, a colleague request — even prose that merely mentions a
"support ticket") must stay needs_reply=True, and the demo must route through
the production classifier. Hermetic: no network, no DB, no model.
"""

from __future__ import annotations

import importlib

from app.agent.inbox_fetch import InboxMessage
from app.agent.needs_reply import classify


def _msg(sender: str, sender_email: str, subject: str, body: str, headers=None) -> InboxMessage:
    return InboxMessage(
        message_id="m",
        thread_id="t",
        account="me@example.com",
        sender=sender,
        sender_email=sender_email,
        subject=subject,
        body=body,
        headers=headers or {},
    )


# --- Transactional / automation: MUST be skipped (needs_reply=False) --------

TRANSACTIONAL_SAMPLES = [
    # [11] conference ticket
    (
        "ticket",
        _msg(
            "DevConf <tickets@devconf.io>", "tickets@devconf.io",
            "Your ticket for DevConf 2026",
            "Your e-ticket is attached. Show this QR code at the entrance. See you there!",
        ),
    ),
    # [14] invoice
    (
        "invoice",
        _msg(
            "Acme Billing <invoices@acme.com>", "invoices@acme.com",
            "Invoice #INV-2026-0042",
            "Please find your invoice attached. Amount due: 240.00. Thanks for your business.",
        ),
    ),
    # [9] event newsletter (with List-Unsubscribe → hard skip)
    (
        "newsletter_with_list_unsub",
        _msg(
            "TechHub <events@techhub.io>", "events@techhub.io",
            "This week at TechHub: 5 events you can join",
            "Join us this week! Mon: AI meetup. Tue: networking. Click to RSVP.",
            headers={"list-unsubscribe": "<mailto:unsub@techhub.io>"},
        ),
    ),
    # [9] event newsletter WITHOUT List-Unsubscribe (the harder case the
    # crude header check would miss) — operational mailbox keeps it below
    # threshold even without the header.
    (
        "newsletter_no_list_unsub",
        _msg(
            "TechHub <events@techhub.io>", "events@techhub.io",
            "This week at TechHub: 5 events you can join",
            "Join us this week! Monday is our AI meetup and Tuesday is networking night.",
        ),
    ),
    # booking confirmation
    (
        "booking_confirmation",
        _msg(
            "Booking <no-reply@booking.com>", "no-reply@booking.com",
            "Booking confirmation",
            "Your booking is confirmed for June 5. Reference ABC123.",
        ),
    ),
    # payment / receipt notice
    (
        "payment_notice",
        _msg(
            "Stripe <receipts@stripe.com>", "receipts@stripe.com",
            "Payment received",
            "Your payment of 50 has been received. Receipt attached.",
        ),
    ),
]


# --- Genuine reply-worthy mail: MUST stay KEEP (needs_reply=True) -----------

GENUINE_SAMPLES = [
    # [5] Fadi — a real person's meeting invite
    (
        "person_meeting_invite",
        _msg(
            "Fadi Saleh <fadi@medicus.ai>", "fadi@medicus.ai",
            "Quick sync tomorrow?",
            "Hi Baher, can we meet tomorrow at 3pm to go over the Q3 roadmap? "
            "Let me know what works.",
        ),
    ),
    # a client question
    (
        "client_question",
        _msg(
            "Sarah Lin <sarah@clientcorp.com>", "sarah@clientcorp.com",
            "Question on the proposal",
            "Hi, could you clarify the pricing in section 3 of the proposal? "
            "We need it before Friday.",
        ),
    ),
    # payroll arrangement needing confirmation
    (
        "payroll_confirmation",
        _msg(
            "Maria HR <maria@medicus.ai>", "maria@medicus.ai",
            "Payroll arrangement",
            "Hi Baher, can you confirm your bank details are unchanged for "
            "this month's payroll run?",
        ),
    ),
    # a colleague request
    (
        "colleague_request",
        _msg(
            "Omar <omar@medicus.ai>", "omar@medicus.ai",
            "Slides for Thursday",
            "Hey Baher, could you please review the deck and send me your edits "
            "before Thursday's board call?",
        ),
    ),
    # prose that merely MENTIONS a support ticket — must not be misclassified
    # as transactional (precision guard for the new ticket/invoice patterns).
    (
        "support_ticket_prose",
        _msg(
            "Joe <joe@partner.com>", "joe@partner.com",
            "re: the issue",
            "I opened a support ticket on your portal last week but no one "
            "replied. Can you escalate this for me?",
        ),
    ),
]


class TestTransactionalSkipped:
    def test_all_transactional_are_not_needs_reply(self):
        for name, msg in TRANSACTIONAL_SAMPLES:
            v = classify(msg, history=None, threshold=0.6)
            assert v.needs_reply is False, (
                f"{name!r} should be SKIP/needs_reply=False but got "
                f"needs_reply=True (score={v.score:.2f}, reasons={v.reasons})"
            )


class TestGenuineKept:
    def test_all_genuine_are_needs_reply(self):
        for name, msg in GENUINE_SAMPLES:
            v = classify(msg, history=None, threshold=0.6)
            assert v.needs_reply is True, (
                f"{name!r} should be KEEP/needs_reply=True but got "
                f"needs_reply=False (score={v.score:.2f}, reasons={v.reasons})"
            )


class TestSpecificGaps:
    """Pin the exact b178 regressions so they can't silently come back."""

    def test_invoice_subject_is_transactional(self):
        from app.agent.needs_reply import TRANSACTIONAL_TEMPLATE_PAT

        assert TRANSACTIONAL_TEMPLATE_PAT.search("Invoice #INV-2026-0042")
        assert TRANSACTIONAL_TEMPLATE_PAT.search("Your receipt from Acme")

    def test_ticket_subject_is_transactional(self):
        from app.agent.needs_reply import TRANSACTIONAL_TEMPLATE_PAT

        assert TRANSACTIONAL_TEMPLATE_PAT.search("Your ticket for DevConf 2026")
        assert TRANSACTIONAL_TEMPLATE_PAT.search("Your tickets are ready")

    def test_support_ticket_prose_not_transactional(self):
        """A human writing 'I opened a support ticket' must NOT trip the
        transactional template — that would drop real mail."""
        from app.agent.needs_reply import TRANSACTIONAL_TEMPLATE_PAT

        assert not TRANSACTIONAL_TEMPLATE_PAT.search(
            "I opened a support ticket and need help"
        )

    def test_operational_mailboxes_penalised(self):
        from app.agent.needs_reply import NON_HUMAN_MAILBOX_PAT

        for addr in (
            "invoices@acme.com", "tickets@devconf.io", "events@techhub.io",
            "receipts@stripe.com", "billing@acme.com", "accounts@vendor.com",
        ):
            assert NON_HUMAN_MAILBOX_PAT.search(addr), addr

    def test_personal_mailbox_not_penalised(self):
        from app.agent.needs_reply import NON_HUMAN_MAILBOX_PAT

        for addr in ("fadi@medicus.ai", "sarah@clientcorp.com", "omar@medicus.ai"):
            assert not NON_HUMAN_MAILBOX_PAT.search(addr), addr


class TestDemoUsesProductionClassifier:
    """The demo (scripts/triage_demo.py) must route its KEEP/SKIP decision
    through the production needs-reply classifier, not a hand-rolled regex."""

    def _load_demo(self):
        import os.path
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = os.path.join(root, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        return importlib.import_module("triage_demo")

    def test_demo_decide_returns_production_verdict_for_transactional(self):
        demo = self._load_demo()
        payload = {
            "headers": [
                {"name": "From", "value": "Acme Billing <invoices@acme.com>"},
                {"name": "Subject", "value": "Invoice #INV-2026-0042"},
            ]
        }
        verdict = demo.decide(
            payload,
            sender="Acme Billing <invoices@acme.com>",
            subject="Invoice #INV-2026-0042",
            body="Please find your invoice attached. Amount due: 240.00.",
        )
        # Same verdict type + decision the production classifier produces.
        from app.agent.needs_reply import NeedsReplyVerdict

        assert isinstance(verdict, NeedsReplyVerdict)
        assert verdict.needs_reply is False

    def test_demo_decide_keeps_genuine_mail(self):
        demo = self._load_demo()
        payload = {
            "headers": [
                {"name": "From", "value": "Fadi Saleh <fadi@medicus.ai>"},
                {"name": "Subject", "value": "Quick sync tomorrow?"},
            ]
        }
        verdict = demo.decide(
            payload,
            sender="Fadi Saleh <fadi@medicus.ai>",
            subject="Quick sync tomorrow?",
            body="Hi Baher, can we meet tomorrow at 3pm? Let me know what works.",
        )
        assert verdict.needs_reply is True

    def test_demo_has_no_handrolled_keep_skip_regex(self):
        """Guard: the old crude rules_skip / AUTOMATION_DOMAINS filter is gone,
        so the demo can't drift from production again."""
        demo = self._load_demo()
        assert not hasattr(demo, "rules_skip")
        assert not hasattr(demo, "AUTOMATION_DOMAINS")
        # It DOES expose a decide() that wraps the real classifier.
        assert hasattr(demo, "decide")
