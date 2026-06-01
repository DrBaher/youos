"""b179: courtesy-on-decline tone guidance + light un-grounded-claim guard.

Two quality issues remained after b173/b174 cleaned up coherence:

1. Declines / cold-outreach rejections drifted from "direct" into rude
   ("your model is a copycat. No value-add."). The system turn now always
   carries a concise COURTESY rule so the model declines politely while
   keeping Baher's tight register — without padding the common case.
2. Drafts occasionally asserted un-grounded specifics ("Logo finalised. Pitch
   draft ready.") absent from the inbound. ``verify_draft`` now WARNS (not
   blocks) on such claims, so the human reviewer sees the flag.

These tests are hermetic (no model, no DB): they assert the system message
includes the courtesy guidance, that the grounding guard flags un-grounded
specifics and passes grounded / clean drafts, and that clean drafts are
otherwise unaffected.
"""

from __future__ import annotations

import app.generation.service as svc
from app.generation.verify import verify_draft


# --------------------------------------------------------------------------
# 1. Tone: the system turn always carries the courtesy/decline guidance.
# --------------------------------------------------------------------------
def _system_text(**overrides) -> str:
    kwargs = dict(
        inbound_message="We'd love to sell you our B2B SaaS sales platform.",
        reply_pairs=[],
        persona={"style": {"voice": "direct, concise", "avg_reply_words": 40}},
        prompts={"system_prompt": "You are BaherOS."},
        sender_type="external_client",
    )
    kwargs.update(overrides)
    msgs = svc.assemble_chat_messages(**kwargs)
    return msgs[0]["content"]


def test_system_message_includes_courtesy_guidance():
    sys_content = _system_text()
    low = sys_content.lower()
    # The courtesy floor must be present and mention declining politely.
    assert "courteous" in low and "professional" in low
    assert "declin" in low
    # It must explicitly guard against rudeness/dismissiveness.
    assert "insult" in low or "dismissive" in low


def test_courtesy_guidance_does_not_force_verbosity_or_flattery():
    sys_content = _system_text()
    low = sys_content.lower()
    # The rule explicitly forbids padding so normal replies stay tight.
    assert "flattery" in low or "filler" in low
    # Direct/concise register is preserved, not replaced by sycophancy.
    assert "concise" in low or "direct" in low


def test_courtesy_guidance_present_regardless_of_inbound():
    # Present on a benign scheduling ask too (it generalizes, not a decline hack).
    sys_content = _system_text(inbound_message="Can we meet Tuesday to review the budget?")
    assert "courteous" in sys_content.lower()


def test_legacy_assemble_prompt_also_carries_courtesy():
    prompt = svc.assemble_prompt(
        inbound_message="We'd love to sell you our platform.",
        reply_pairs=[],
        persona={"style": {"voice": "direct, concise", "avg_reply_words": 40}},
        prompts={"system_prompt": "You are BaherOS."},
        sender_type="external_client",
    )
    assert "[COURTESY]" in prompt
    assert "courteous" in prompt.lower()


# --------------------------------------------------------------------------
# 2. Grounding guard: flag un-grounded specifics, pass grounded / clean.
# --------------------------------------------------------------------------
def test_grounding_guard_flags_invented_deliverable():
    # The classic regression: a draft invents finished deliverables that the
    # inbound never mentioned.
    inbound = "Hey, how's the new brand project going? Any update?"
    draft = "Going well. Logo finalised. Pitch draft ready. Talk soon."
    res = verify_draft(draft, inbound=inbound)
    # Not blocking (human reviews) — surfaced as warnings.
    assert res.ok is True
    joined = " ".join(res.warnings).lower()
    assert "unsupported claim" in joined
    # Both the "finalised" and a "ready" claim should be caught.
    assert "finalis" in joined or "ready" in joined


def test_grounding_guard_flags_invented_attachment():
    inbound = "Could you send me the latest numbers when you get a chance?"
    draft = "Sure — I've attached the full breakdown for you."
    res = verify_draft(draft, inbound=inbound)
    assert res.ok is True
    assert any("unsupported claim" in w.lower() for w in res.warnings)


def test_grounding_guard_passes_grounded_claim():
    # When the inbound itself mentions the attachment, "attached" is grounded.
    inbound = "I've attached our proposal — can you confirm you received it?"
    draft = "Got it, thanks — reviewing the attached proposal now."
    res = verify_draft(draft, inbound=inbound)
    assert res.ok is True
    assert not any("unsupported claim" in w.lower() for w in res.warnings)


def test_grounding_guard_passes_grounded_via_thread():
    inbound = "Where do things stand?"
    thread = [{"sender": "x", "text": "The logo is being finalised this week."}]
    draft = "The logo is finalised — sending it over today."
    res = verify_draft(draft, inbound=inbound, thread_history=thread)
    assert res.ok is True
    assert not any("unsupported claim" in w.lower() for w in res.warnings)


def test_grounding_guard_leaves_clean_drafts_unaffected():
    # A polite decline with no invented specifics must produce no claim warnings
    # and remain non-blocking.
    inbound = "We'd love to sell you our B2B SaaS sales platform — interested?"
    draft = (
        "Thanks for reaching out. This isn't a fit for us right now, "
        "but I appreciate you thinking of us. Best of luck."
    )
    res = verify_draft(draft, inbound=inbound)
    assert res.ok is True
    assert not any("unsupported claim" in w.lower() for w in res.warnings)


def test_grounding_guard_does_not_break_existing_email_block():
    # The pre-existing invented-email blocking check must still fire.
    inbound = "Can you confirm the meeting?"
    draft = "Sure, email me at made-up@nowhere.example to confirm."
    res = verify_draft(draft, inbound=inbound)
    assert res.ok is False
    assert any("invented email" in b.lower() for b in res.blocking)
