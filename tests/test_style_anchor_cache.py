from __future__ import annotations

from app.generation.service import (
    _apply_cached_order,
    _cache_key,
    _get_cached_exemplar_ids,
    _top_exemplar_source_ids,
    _update_exemplar_cache,
    assemble_prompt,
    clear_exemplar_cache,
)
from app.retrieval.service import RetrievalMatch


def _rp(source_id: str, score: float = 5.0, quality_score: float = 1.0) -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="gmail",
        source_id=source_id,
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        inbound_text=f"inbound {source_id}",
        reply_text=f"reply {source_id}",
        snippet=f"snippet {source_id}",
        metadata={"quality_score": quality_score},
    )


def test_style_anchor_is_included_for_sender_type(monkeypatch):
    monkeypatch.setattr(
        "app.generation.service.get_persona_style_anchor",
        lambda sender_type: "Use crisp, executive language." if sender_type == "client" else None,
    )
    prompt = assemble_prompt(
        inbound_message="Can we align on next steps?",
        reply_pairs=[],
        persona={"style": {"voice": "direct"}},
        prompts={"system_prompt": "You are YouOS."},
        sender_type="client",
    )
    assert "[STYLE ANCHOR — client]" in prompt
    assert "Use crisp, executive language." in prompt


def test_exemplar_cache_hit_miss_and_key():
    clear_exemplar_cache()
    ids, hit, key = _get_cached_exemplar_ids("follow_up", "client")
    assert ids == []
    assert hit is False
    assert key == "follow_up::client"

    _update_exemplar_cache("follow_up", "client", ["a", "b"])
    ids2, hit2, key2 = _get_cached_exemplar_ids("follow_up", "client")
    assert hit2 is True
    assert ids2 == ["a", "b"]
    assert key2 == "follow_up::client"


def test_apply_cached_order_reorders_matches():
    rps = [_rp("x"), _rp("y"), _rp("z")]
    ordered = _apply_cached_order(rps, ["z", "x"])
    assert [rp.source_id for rp in ordered] == ["z", "x", "y"]


def test_top_exemplar_source_ids_prefers_quality_then_score():
    rps = [
        _rp("low", score=9.0, quality_score=0.5),
        _rp("high", score=6.0, quality_score=1.2),
        _rp("mid", score=7.0, quality_score=1.0),
    ]
    ids = _top_exemplar_source_ids(rps, limit=2)
    assert ids == ["high", "mid"]


def test_cache_key_normalization():
    assert _cache_key(" Follow_Up ", " Client ") == ("follow_up", "client")
