from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.diff import similarity_ratio
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


@_BOOKMARKLET_ROUTER.get("/bookmarklet", response_class=HTMLResponse)
def bookmarklet_page(request: Request) -> HTMLResponse:
    base_url = str(request.base_url).rstrip("/")
    html = BOOKMARKLET_TEMPLATE.read_text(encoding="utf-8")
    # Replace hardcoded localhost URL with actual server URL
    html = html.replace("http://localhost:8765/feedback", f"{base_url}/feedback")
    return HTMLResponse(content=html)


class GenerateBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    tone_hint: Literal["shorter", "more_formal", "more_detail"] | None = None
    sender: str | None = None


@router.post("/generate")
def feedback_generate(body: GenerateBody, request: Request) -> dict:
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
    return {
        "draft": response.draft,
        "precedent_used": response.precedent_used,
        "confidence": response.confidence,
    }


class SubmitBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    generated_draft: str = Field(min_length=1)
    edited_reply: str = Field(min_length=1)
    feedback_note: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)


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
        total = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
    finally:
        conn.close()
    return {"status": "saved", "total_pairs": total, "edit_distance_pct": edit_distance_pct}
