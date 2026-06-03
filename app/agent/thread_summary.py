"""Long-thread "what changed" summaries.

For a reply on a long thread, a 2–3 line catch-up ("what's been decided /
what's open") saves the reviewer from re-reading the whole conversation. Uses
the warm local model (on-device, no egress); failure-isolated — no summary is
fine, never blocks drafting.

Gated by thread length so we don't spend model time summarizing a two-message
exchange. The transcript is built from the structured ``thread_history`` the
agent already carries on each ``InboxMessage`` (b69), so no re-fetch.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MAX_TURNS = 8          # cap how much of the thread we feed the model
_MAX_TURN_CHARS = 400
_MAX_TRANSCRIPT_CHARS = 4000


def summarize_thread(
    thread_history: list[dict[str, str]] | None,
    *,
    subject: str | None = None,
    min_messages: int = 4,
    max_tokens: int = 150,
) -> str | None:
    """Return a short catch-up summary of the thread, or None.

    None when: the thread is shorter than ``min_messages``; the warm model
    server is disabled/unavailable; or the call fails. Local-only — never falls
    back to the cloud for a summary."""
    if not thread_history or len(thread_history) < min_messages:
        return None

    from app.core import model_server

    if not model_server.is_enabled():
        return None

    lines: list[str] = []
    for h in thread_history[-_MAX_TURNS:]:
        sender = (h.get("sender") or "").strip()[:60]
        text = " ".join((h.get("text") or "").split())[:_MAX_TURN_CHARS]
        if text:
            lines.append(f"{sender}: {text}" if sender else text)
    transcript = "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]
    if not transcript:
        return None

    prompt = (
        "Summarize this email thread in 2-3 short lines so the reader can catch "
        "up fast: what's been decided and what's still open. Be factual and "
        "concise; no preamble, no greeting.\n\n"
        f"Subject: {subject or '(none)'}\n\nThread:\n{transcript}\n\nSummary:"
    )
    try:
        out = model_server.complete(prompt, max_tokens=max_tokens, temperature=0.2)
    except Exception as exc:
        logger.info("thread summary skipped (model unavailable): %s", exc)
        return None
    out = (out or "").strip()
    if not out:
        return None

    # b191: grounding check. A catch-up summary is read at a glance and trusted;
    # a hallucinated date/amount/name in it is worse than no summary at all. So
    # we verify the summary's specifics against the thread transcript (the
    # source) and SUPPRESS (return None — the same as the unavailable path) on
    # poor grounding rather than surface a misleading catch-up. Strict here
    # (max_ungrounded=0): unlike the digest, there is no faithful fallback to
    # degrade to, so any clear fabrication suppresses. Local-only, no egress;
    # failure-isolated so a grounding error keeps current behavior (the summary).
    try:
        from app.agent.summary_grounding import check_summary_grounding

        gr = check_summary_grounding(out, transcript, max_ungrounded=0)
        if not gr.grounded:
            logger.info(
                "thread summary suppressed: failed grounding (score=%.2f, ungrounded=%s)",
                gr.score, gr.ungrounded[:5],
            )
            return None
        # Record the score on the kept path too — otherwise the quality signal is
        # only ever observed on rejection, hiding how well-grounded the summaries
        # we *do* surface actually are (i.e. whether the check has any headroom).
        logger.info("thread summary kept (grounding score=%.2f, checked=%d)", gr.score, gr.checked)
    except Exception as exc:
        logger.info("thread summary grounding check errored, keeping summary: %s", exc)
    return out
