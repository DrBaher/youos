from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.diff import similarity_ratio
from app.core.facts_extractor import extract_and_save
from app.core.rate_limit import RATE_LIMIT_RESPONSE, draft_limiter
from app.db.bootstrap import resolve_sqlite_path
from app.generation.service import DraftRequest, generate_draft

router = APIRouter(prefix="/feedback", tags=["feedback"])

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
TEMPLATE_PATH = TEMPLATE_DIR / "feedback.html"


def _get_db_path(request: Request) -> Path:
    return resolve_sqlite_path(request.app.state.settings.database_url)


@router.get("", response_class=HTMLResponse)
def feedback_page() -> HTMLResponse:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


# Bookmarklet install page — mounted outside /feedback prefix via app-level include
_BOOKMARKLET_ROUTER = APIRouter(tags=["bookmarklet"])
BOOKMARKLET_TEMPLATE = TEMPLATE_DIR / "bookmarklet.html"
POPUP_TEMPLATE = TEMPLATE_DIR / "draft_popup.html"

ABOUT_TEMPLATE = TEMPLATE_DIR / "about.html"


@_BOOKMARKLET_ROUTER.get("/about", response_class=HTMLResponse)
def about_page() -> HTMLResponse:
    html = ABOUT_TEMPLATE.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@_BOOKMARKLET_ROUTER.get("/bookmarklet", response_class=HTMLResponse)
def bookmarklet_page(request: Request) -> HTMLResponse:
    base_url = str(request.base_url).rstrip("/")
    html = BOOKMARKLET_TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("http://localhost:8765/feedback", f"{base_url}/feedback")
    html = html.replace("http://localhost:8765/draft-popup", f"{base_url}/draft-popup")
    html = html.replace("YOUOS_BASE_URL", base_url)
    return HTMLResponse(content=html)


@_BOOKMARKLET_ROUTER.get("/draft-popup", response_class=HTMLResponse)
def draft_popup_page() -> HTMLResponse:
    """Minimal popup-optimised draft UI, embedded as iframe in Gmail."""
    html = POPUP_TEMPLATE.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


class GenerateBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    tone_hint: Literal["shorter", "more_formal", "more_detail"] | None = None
    sender: str | None = None


@router.post("/generate")
def feedback_generate(body: GenerateBody, request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not draft_limiter.is_allowed(client_ip):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=429, content=RATE_LIMIT_RESPONSE)
    settings = request.app.state.settings
    response = generate_draft(
        DraftRequest(
            inbound_message=body.inbound_text,
            tone_hint=body.tone_hint,
            sender=body.sender,
        ),
        database_url=settings.database_url,
        configs_dir=settings.configs_dir,
    )

    # Save to draft_history
    try:
        db_path = _get_db_path(request)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO draft_history
                   (inbound_text, sender, generated_draft, confidence, model_used, retrieval_method)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.inbound_text, body.sender, response.draft, response.confidence, response.model_used, response.retrieval_method),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Don't fail the request if history save fails

    return {
        "draft": response.draft,
        "precedent_used": response.precedent_used,
        "confidence": response.confidence,
        "confidence_warning": response.confidence == "low",
        "suggested_subject": response.suggested_subject,
    }


class SubmitBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    generated_draft: str = Field(min_length=1)
    edited_reply: str = Field(min_length=1)
    feedback_note: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    sender: str | None = None


@router.post("/submit")
def feedback_submit(body: SubmitBody, request: Request) -> dict:
    db_path = _get_db_path(request)
    edit_distance_pct = round(1.0 - similarity_ratio(body.generated_draft, body.edited_reply), 4)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO feedback_pairs
                (inbound_text, generated_draft, edited_reply, feedback_note, rating,
                 edit_distance_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                body.inbound_text,
                body.generated_draft,
                body.edited_reply,
                body.feedback_note,
                body.rating,
                edit_distance_pct,
            ),
        )
        conn.commit()

        # Update quality_score on linked reply_pair if reply_pair_id exists
        try:
            last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = conn.execute("SELECT reply_pair_id, rating, edit_distance_pct FROM feedback_pairs WHERE id = ?", (last_id,)).fetchone()
            if row and row[0] is not None and row[1] is not None:
                rp_id = row[0]
                rating = row[1]
                edp = row[2] or 0.0
                quality_score = (rating / 5.0) * (1.0 - edp) + 0.3
                quality_score = max(0.3, min(1.3, quality_score))
                conn.execute("UPDATE reply_pairs SET quality_score = ? WHERE id = ?", (round(quality_score, 4), rp_id))
                conn.commit()
        except Exception:
            pass  # Don't fail if quality_score column doesn't exist yet

        total = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
    finally:
        conn.close()

    # Extract and auto-save facts from the feedback note
    extracted_facts: list[dict] = []
    if body.feedback_note:
        try:
            extracted_facts = extract_and_save(body.feedback_note, db_path, sender_email=body.sender)
        except Exception:
            pass

    return {
        "status": "saved",
        "total_pairs": total,
        "edit_distance_pct": edit_distance_pct,
        "extracted_facts": extracted_facts,
    }
