"""Keep drafting on-device: retry the local model once before the cloud, and
cap a huge inbound so it can't overflow into a cloud fallback.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.generation.service import _max_inbound_chars


def _stub(monkeypatch, *, local_draft_once, fallback="claude", claude="Cloud reply text here."):
    """Stub generate_draft's I/O so only the local-path + empty-retry branch
    matters. ``local_draft_once`` is the (draft, model) producer to install."""
    from app.generation import service as svc

    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {"avg_reply_words": 30}, "modes": {}})
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(svc, "_connect", lambda _p: sqlite3.connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "get_model_fallback", lambda *a, **k: fallback)
    monkeypatch.setattr(svc, "_multi_candidate_config", lambda: {"enabled": False, "temperatures": [0.7]})
    monkeypatch.setattr(svc, "_local_draft_once", local_draft_once)
    monkeypatch.setattr(svc, "_call_claude_cli", lambda *a, **kw: claude)
    return svc


# --- inbound length cap ----------------------------------------------------


def test_max_inbound_chars_default():
    assert _max_inbound_chars() == 6000


def test_huge_inbound_is_truncated_before_the_model(monkeypatch):
    """A very long email is trimmed in the prompt the local model receives, so
    it can't overflow context and force a cloud fallback."""
    seen = {}

    def _capture(prompt, **kw):
        seen["prompt"] = prompt
        return ("Confirmed, thanks. Best, B", "qwen2.5-1.5b-lora")

    svc = _stub(monkeypatch, local_draft_once=_capture)
    monkeypatch.setattr(svc, "_max_inbound_chars", lambda: 300)

    huge = "Meeting notes line. " * 500  # ~10k chars
    svc.generate_draft(
        svc.DraftRequest(inbound_message=huge, use_local_model=True, use_adapter=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    # b173: the local path now hands _local_draft_once a ChatML messages list
    # ([{role, content}, ...]) rather than a single string. The truncation marker
    # lives inside a message's `content`, so flatten to text before asserting.
    captured = seen["prompt"]
    if isinstance(captured, list):
        prompt_text = "\n".join(str(m.get("content", "")) for m in captured)
    else:
        prompt_text = captured
    assert "message truncated" in prompt_text
    # The inbound section is bounded, not the full 10k.
    assert prompt_text.count("Meeting notes line.") < 60


# --- local-retry-before-cloud (the privacy fix) ----------------------------


def test_empty_local_retries_locally_then_recovers(monkeypatch):
    calls = {"n": 0}

    def _flaky(prompt, **kw):
        calls["n"] += 1
        return ("", "qwen2.5-1.5b-lora") if calls["n"] == 1 else ("Hi — confirmed, talk soon. Best, B", "qwen2.5-1.5b-lora")

    svc = _stub(monkeypatch, local_draft_once=_flaky,
                claude="MUST NOT BE USED")
    monkeypatch.setattr(svc, "_call_claude_cli",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cloud must not be hit after a good local retry")))

    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="Could you confirm Q3 pricing?", use_local_model=True, use_adapter=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert calls["n"] == 2                      # retried locally exactly once
    assert "lora" in resp.model_used            # stayed on-device
    assert resp.empty_output_retried is False
    assert resp.draft.strip()


def test_empty_local_twice_falls_back_to_cloud(monkeypatch):
    svc = _stub(monkeypatch,
                local_draft_once=lambda prompt, **kw: ("", "qwen2.5-1.5b-lora"),
                fallback="claude", claude="Cloud-drafted reply.")
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="Could you confirm Q3 pricing?", use_local_model=True, use_adapter=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used == "claude"
    assert resp.empty_output_retried is True
    assert resp.draft == "Cloud-drafted reply."


def test_strict_local_empty_does_not_touch_cloud(monkeypatch):
    """With strict_local, an empty local draft (even after retry) must NOT fall
    back to the cloud — it raises instead."""
    import pytest

    svc = _stub(monkeypatch, local_draft_once=lambda prompt, **kw: ("", "qwen2.5-1.5b-lora"))
    monkeypatch.setattr(svc, "_call_claude_cli",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("strict_local must never hit cloud")))
    with pytest.raises(ValueError):
        svc.generate_draft(
            svc.DraftRequest(inbound_message="hi", use_local_model=True, use_adapter=True, strict_local=True),
            database_url="sqlite:///x", configs_dir=Path("/tmp"),
        )


def test_generate_subject_honors_strict_local_no_cloud_egress(monkeypatch):
    """b139: generate_subject must honor the per-request fallback_model. Under
    strict_local (fallback_model='none') it must NOT call the cloud Claude CLI
    even when the local model is down and the GLOBAL fallback says 'claude'."""
    from app.generation import service as svc

    calls = {"claude": 0}
    monkeypatch.setattr(svc, "_subject_fallback", lambda t: None)       # force the model branch
    monkeypatch.setattr(svc, "_local_model_available", lambda: False)   # no local model
    monkeypatch.setattr(svc, "get_model_fallback", lambda: "claude")    # GLOBAL says claude
    monkeypatch.setattr(svc, "_call_claude_cli",
                        lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or "leaked")

    # strict_local: the inbound body must NOT reach the cloud.
    assert svc.generate_subject("private", "draft", "sqlite:///x", Path("."), fallback_model="none") is None
    assert calls["claude"] == 0

    # explicit claude fallback still uses the cloud (interactive path intact).
    assert svc.generate_subject("body", "draft", "sqlite:///x", Path("."), fallback_model="claude") == "leaked"
    assert calls["claude"] == 1

    # a non-claude/non-local fallback (e.g. ollama) must not silently egress to claude.
    calls["claude"] = 0
    assert svc.generate_subject("body", "draft", "sqlite:///x", Path("."), fallback_model="ollama") is None
    assert calls["claude"] == 0


def test_thread_context_widened_to_six_turns():
    """Thread context now surfaces up to 6 prior turns (was 4) so the drafter
    sees more of the conversation (personal remarks, prior commitments)."""
    from app.generation.service import _format_thread_context
    hist = [{"sender": f"s{i}", "text": f"msg {i}"} for i in range(8)]
    out = _format_thread_context("CURRENT MESSAGE", hist)
    assert out.count("Previous:") == 6
    assert "[CURRENT MESSAGE]" in out and "CURRENT MESSAGE" in out
