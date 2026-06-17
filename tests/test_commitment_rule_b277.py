"""b277: anti-over-commitment guard in the draft system prompt.

The local model freely invented firm commitments ("I'll send it over tomorrow",
"it's done") the user never made — the #1 issue on the replay backtest and a top
driver of the high rewrite distance. The system turn now always carries a concise
COMMITMENT rule: when a reply would promise a specific date/deliverable it can't
be sure of, hedge or defer instead. Phrased conditionally (so it doesn't bite
ordinary replies) with a "don't over-hedge" guard.

Hermetic: no model, no DB — assert the rule is present in both prompt builders.
"""

from __future__ import annotations

import app.generation.service as svc


def _system_text(**overrides) -> str:
    kwargs = dict(
        inbound_message="Can you sign the invoice and send it back?",
        reply_pairs=[],
        persona={"style": {"voice": "direct, concise", "avg_reply_words": 40}},
        prompts={"system_prompt": "You are BaherOS."},
        sender_type="external_client",
    )
    kwargs.update(overrides)
    return svc.assemble_chat_messages(**kwargs)[0]["content"]


def test_commitment_rule_present_in_system_turn():
    low = _system_text().lower()
    assert "commitment" in low or "commit" in low
    # The core instruction: hedge/defer rather than promise a firm timeline.
    assert "hedge" in low or "defer" in low
    assert "follow up" in low  # the suggested deferral phrasing


def test_commitment_rule_guards_against_over_hedging():
    # Must not tip drafts into wishy-washy: the rule explicitly says keep brief.
    low = _system_text().lower()
    assert "over-hedge" in low or "don't over-hedge" in low or "keep it brief" in low


def test_commitment_rule_present_regardless_of_inbound():
    # Always-on, like the courtesy rule — generalizes, not a per-intent hack.
    low = _system_text(inbound_message="Thanks for the update, looks good.").lower()
    assert "hedge" in low or "defer" in low


def test_legacy_assemble_prompt_also_carries_commitment():
    prompt = svc.assemble_prompt(
        inbound_message="Can you put together the Q3 summary and send it over?",
        reply_pairs=[],
        persona={"style": {"voice": "direct, concise", "avg_reply_words": 40}},
        prompts={"system_prompt": "You are BaherOS."},
        sender_type="external_client",
    )
    assert "[COMMITMENT]" in prompt
    assert "hedge" in prompt.lower() or "defer" in prompt.lower()
