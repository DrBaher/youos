"""b191: grounding / hallucination check for summaries.

Covers the summary grounding heuristic itself and its wiring into the digest
(fall back to the deterministic plain list) and the thread catch-up summary
(suppress → None). The check is local-only, deterministic, language-agnostic,
and conservative (flags clear fabrications, not faithful paraphrase).
"""

from __future__ import annotations

import app.agent.digest_tasks as dt
import app.agent.thread_summary as ts
from app.agent.summary_grounding import GroundingResult, check_summary_grounding

# --- the heuristic ----------------------------------------------------------

def test_faithful_summary_is_grounded():
    """(i) Every specific in the summary is present in the source → grounded."""
    src = "- From Acme Corp | Invoice 1,250.00 EUR due 2026-06-15 | Mon"
    summary = "Acme Corp sent an invoice for 1,250.00 EUR due 2026-06-15."
    gr = check_summary_grounding(summary, src)
    assert gr.grounded is True
    assert gr.ungrounded == []
    assert gr.score == 1.0


def test_invented_number_and_date_flagged():
    """(ii) A fabricated amount + date absent from the source → not grounded."""
    src = "- From Acme Corp | Invoice 1,250.00 EUR due 2026-06-15 | Mon"
    summary = "Acme Corp sent an invoice for 9,999.00 EUR due 2026-12-31."
    gr = check_summary_grounding(summary, src)
    assert gr.grounded is False
    assert any("9,999" in u for u in gr.ungrounded)
    assert gr.score < 1.0


def test_invented_name_flagged_strict():
    """A fabricated multi-word proper noun is caught under the strict (0) gate."""
    src = "- From Acme Corp | Invoice 1,250.00 EUR due 2026-06-15 | Mon"
    summary = "Jane Doe sent an invoice for 1,250.00 EUR due 2026-06-15."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is False
    assert any("Jane Doe" in u for u in gr.ungrounded)


def test_abstractive_paraphrase_not_flagged():
    """(iii) A faithful paraphrase that introduces NO new specific is not
    flagged — guards against over-triggering on honest summarization."""
    src = "- From Acme Corp | Invoice 1,250.00 EUR due 2026-06-15 | Mon"
    summary = "An invoice arrived and needs attention soon."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is True
    assert gr.checked == 0  # no checkable specific introduced
    assert gr.score == 1.0


def test_reformatted_date_not_false_flagged():
    """A faithful summary that REFORMATS a source date/time still matches via the
    digit-signature normalization (not penalized as fabrication)."""
    src = "Jane: The meeting is on 2026-06-15 at 14:00. Budget is 1250 EUR."
    summary = "Meeting set for June 15 2026 at 14:00; budget 1250 EUR."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is True
    assert gr.ungrounded == []


def test_multilingual_de_amount_matched():
    """(iv) German source: a real amount with comma-decimal / dot-thousands is
    matched against an EN-ish summary, not falsely flagged."""
    src = "- Von Müller GmbH | Rechnung über 2.450,00 EUR fällig am 15.06.2026 | Mo"
    summary = "Müller GmbH: Rechnung 2.450,00 EUR."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is True
    assert gr.ungrounded == []


def test_multilingual_fr_amount_matched():
    """French source: real amount/date matched."""
    src = "- De Société Générale | Facture de 1 250,00 EUR à payer | mardi"
    summary = "Société Générale a envoyé une facture de 1 250,00 EUR."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is True


def test_multilingual_fr_invented_amount_flagged():
    """French source, but the summary states an amount that isn't there."""
    src = "- De Société Générale | Facture de 1 250,00 EUR à payer | mardi"
    summary = "Société Générale a envoyé une facture de 7 777,00 EUR."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is False
    assert any("7" in u and "777" in u for u in gr.ungrounded)


def test_empty_summary_is_grounded():
    assert check_summary_grounding("", "anything").grounded is True
    assert check_summary_grounding("   ", "anything").checked == 0


def test_url_and_email_fabrication_flagged():
    src = "Contact us at help@acme.test for details."
    summary = "Email evil@phish.test or visit https://phish.test/login now."
    gr = check_summary_grounding(summary, src, max_ungrounded=0)
    assert gr.grounded is False
    assert any("phish.test" in u for u in gr.ungrounded)


# --- digest wiring ----------------------------------------------------------

_ITEMS = [
    {"id": "m1", "from": "Acme Corp <billing@acme.test>", "subject": "Invoice 1,250.00 EUR due 2026-06-15", "date": "Mon"},
]


def test_digest_keeps_grounded_model_summary():
    """A faithful model summary is kept (prose present, plain list appended)."""
    def fake_complete(_prompt):
        return "Acme Corp sent an invoice for 1,250.00 EUR due 2026-06-15."

    body = dt.build_digest_body(_ITEMS, model="local", complete_fn=fake_complete)
    assert "Acme Corp sent an invoice" in body
    assert "— items —" in body  # the plain list is appended after kept prose


def test_digest_falls_back_to_plain_list_on_fabrication():
    """(ii-digest) A model summary with an invented amount/date is dropped and the
    deterministic plain list is used instead."""
    def fake_complete(_prompt):
        return "URGENT: wire 9,999.00 EUR to account by 2027-01-01 immediately."

    body = dt.build_digest_body(_ITEMS, model="local", complete_fn=fake_complete)
    # The fabricated prose must NOT appear …
    assert "9,999.00" not in body
    assert "2027-01-01" not in body
    # … and the faithful plain listing (always grounded) must be present.
    assert "Worth attention" in body
    assert "billing@acme.test" in body or "Invoice 1,250.00 EUR" in body


def test_digest_grounding_exception_keeps_summary(monkeypatch):
    """(v) If the grounding check itself errors, the digest keeps the model
    summary (failure-safe — never crash the digest path)."""
    def boom(*_a, **_k):
        raise RuntimeError("grounding exploded")

    monkeypatch.setattr("app.agent.summary_grounding.check_summary_grounding", boom)

    def fake_complete(_prompt):
        return "Acme Corp sent an invoice for 1,250.00 EUR due 2026-06-15."

    body = dt.build_digest_body(_ITEMS, model="local", complete_fn=fake_complete)
    assert "Acme Corp sent an invoice" in body  # kept despite the grounding error


# --- thread-summary wiring --------------------------------------------------

_THREAD = [
    {"sender": "Jane", "text": "Can we ship the v2 API on 2026-06-15?"},
    {"sender": "Bob", "text": "Yes, budget approved at 1250 EUR for the work."},
    {"sender": "Jane", "text": "Great, let's confirm the 2026-06-15 date then."},
    {"sender": "Bob", "text": "Confirmed. I'll start Monday."},
]


def _enable_model(monkeypatch, completion):
    from app.core import model_server

    monkeypatch.setattr(model_server, "is_enabled", lambda: True)
    monkeypatch.setattr(model_server, "complete", completion)


def test_thread_summary_kept_when_grounded(monkeypatch):
    _enable_model(monkeypatch, lambda *a, **k: "Jane and Bob confirmed the v2 API for 2026-06-15, budget 1250 EUR.")
    out = ts.summarize_thread(_THREAD, subject="v2 API")
    assert out is not None
    assert "2026-06-15" in out


def test_thread_summary_suppressed_on_fabrication(monkeypatch):
    """(ii-thread) A summary with an invented date/amount is SUPPRESSED (None)
    rather than surfaced — a wrong catch-up is worse than none."""
    _enable_model(monkeypatch, lambda *a, **k: "Jane and Bob agreed to ship on 2099-12-31 for 50000 EUR.")
    out = ts.summarize_thread(_THREAD, subject="v2 API")
    assert out is None


def test_thread_summary_abstractive_not_suppressed(monkeypatch):
    """(iii-thread) A faithful abstractive summary (no new specifics) is kept."""
    _enable_model(monkeypatch, lambda *a, **k: "The team agreed to ship the next API version with an approved budget.")
    out = ts.summarize_thread(_THREAD, subject="v2 API")
    assert out is not None


def test_thread_summary_grounding_exception_keeps_summary(monkeypatch):
    """(v) Grounding-check exception → keep the model summary (failure-safe)."""
    _enable_model(monkeypatch, lambda *a, **k: "Jane and Bob confirmed the v2 API for 2026-06-15.")

    def boom(*_a, **_k):
        raise RuntimeError("grounding exploded")

    monkeypatch.setattr("app.agent.summary_grounding.check_summary_grounding", boom)
    out = ts.summarize_thread(_THREAD, subject="v2 API")
    assert out is not None  # not crashed, not suppressed


def test_thread_summary_none_when_model_off(monkeypatch):
    """(vi) Read-only / model-off path is unchanged — None, no send, no action."""
    from app.core import model_server

    monkeypatch.setattr(model_server, "is_enabled", lambda: False)
    out = ts.summarize_thread(_THREAD, subject="v2 API")
    assert out is None


def test_thread_summary_below_min_messages_unchanged(monkeypatch):
    """Short threads still short-circuit to None before any model/grounding."""
    _enable_model(monkeypatch, lambda *a, **k: "should not be reached")
    out = ts.summarize_thread(_THREAD[:2], subject="v2 API", min_messages=4)
    assert out is None


# --- no-egress / determinism ------------------------------------------------

def test_grounding_is_pure_no_network():
    """The grounding check makes no model/network call: it returns a plain
    GroundingResult from strings alone, deterministically."""
    a = check_summary_grounding("pay 100 EUR", "invoice for 100 EUR")
    b = check_summary_grounding("pay 100 EUR", "invoice for 100 EUR")
    assert isinstance(a, GroundingResult)
    assert (a.grounded, a.score, a.ungrounded, a.checked) == (b.grounded, b.score, b.ungrounded, b.checked)
