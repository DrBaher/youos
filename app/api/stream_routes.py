from __future__ import annotations

import json
import subprocess
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.rate_limit import RATE_LIMIT_RESPONSE, draft_limiter
from app.core.sender import classify_sender, extract_domain
from app.core.text_utils import strip_quoted_text
from app.generation.service import (
    DraftRequest,
    _apply_cached_order,
    _format_sender_context,
    _get_cached_exemplar_ids,
    _load_persona,
    _load_prompts,
    _precedent_summary,
    _score_confidence,
    _top_exemplar_source_ids,
    _update_exemplar_cache,
    assemble_prompt,
    generate_draft,
    lookup_sender_profile,
)
from app.retrieval.service import RetrievalRequest, retrieve_context

router = APIRouter(prefix="/draft", tags=["draft-stream"])


class StreamBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    tone_hint: Literal["shorter", "more_formal", "more_detail"] | None = None
    sender: str | None = None
    mode: Literal["reply", "compose"] | None = "reply"
    user_prompt: str | None = None


def _stream_generate(body: StreamBody, settings):
    """Generator that yields SSE events."""
    clean_inbound = strip_quoted_text(body.inbound_text)

    sender_type_hint = None
    sender_domain_hint = None
    if body.sender:
        sender_type_hint = classify_sender(body.sender)
        sender_domain_hint = extract_domain(body.sender)

    retrieval_response = retrieve_context(
        RetrievalRequest(
            query=clean_inbound,
            scope="all",
            top_k_reply_pairs=5,
            top_k_chunks=3,
            sender_type_hint=sender_type_hint,
            sender_domain_hint=sender_domain_hint,
        ),
        database_url=settings.database_url,
        configs_dir=settings.configs_dir,
    )

    reply_pairs = retrieval_response.reply_pairs

    # Apply exemplar caching (read + write)
    from app.core.intent import classify_intents_multi
    intents = classify_intents_multi(clean_inbound)
    detected_intent = intents[0]

    cached_ids, exemplar_cache_hit, exemplar_cache_key = _get_cached_exemplar_ids(
        detected_intent,
        sender_type_hint,
        database_url=settings.database_url,
    )
    reply_pairs = _apply_cached_order(reply_pairs, cached_ids)

    selected_ids = _top_exemplar_source_ids(reply_pairs)
    _update_exemplar_cache(
        detected_intent,
        sender_type_hint,
        selected_ids,
        database_url=settings.database_url,
    )

    confidence, _ = _score_confidence(reply_pairs)
    precedent_used = [_precedent_summary(rp) for rp in reply_pairs]
    detected_mode = retrieval_response.detected_mode

    prompts = _load_prompts(settings.configs_dir)
    persona = _load_persona(settings.configs_dir)

    sender_context = None
    if body.sender:
        sender_profile = lookup_sender_profile(body.sender, settings.database_url)
        if sender_profile:
            sender_context = _format_sender_context(sender_profile)

    prompt = assemble_prompt(
        inbound_message=clean_inbound,
        reply_pairs=reply_pairs,
        persona=persona,
        prompts=prompts,
        detected_mode=detected_mode,
        tone_hint=body.tone_hint,
        sender_context=sender_context,
        sender_type=sender_type_hint,
        user_prompt=body.user_prompt,
    )

    # Try streaming via claude CLI subprocess
    try:
        proc = subprocess.Popen(
            ["claude", "--print", prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for line in proc.stdout:
            token = line.rstrip("\n")
            if token:
                yield f"data: {json.dumps({'token': token})}\n\n"
        proc.wait(timeout=120)
        if proc.returncode != 0:
            raise RuntimeError("claude CLI failed")
    except Exception:
        # Fallback: generate full draft non-streaming
        try:
            response = generate_draft(
                DraftRequest(
                    inbound_message=body.inbound_text,
                    tone_hint=body.tone_hint,
                    sender=body.sender,
                    mode=body.mode,
                ),
                database_url=settings.database_url,
                configs_dir=settings.configs_dir,
            )
            yield f"data: {json.dumps({'token': response.draft})}\n\n"
            confidence = response.confidence
            precedent_used = response.precedent_used
        except Exception as exc:
            yield f"data: {json.dumps({'token': f'[generation failed: {exc}]'})}\n\n"

    done_payload = {
        "done": True,
        "confidence": confidence,
        "precedent_used": precedent_used,
        "exemplar_cache_hit": exemplar_cache_hit,
        "exemplar_cache_key": exemplar_cache_key,
    }
    yield f"data: {json.dumps(done_payload)}\n\n"


@router.post("/stream")
def draft_stream(body: StreamBody, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not draft_limiter.is_allowed(client_ip):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=429, content=RATE_LIMIT_RESPONSE)
    settings = request.app.state.settings
    return StreamingResponse(
        _stream_generate(body, settings),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
