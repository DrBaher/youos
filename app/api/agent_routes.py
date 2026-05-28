"""Agent triage API + `/triage` page route.

REST surfaces for the autonomous-agent loop's pending drafts: list them,
amend, dismiss, mark sent, trigger a fresh triage run. The page itself
serves ``templates/triage.html``.

Never auto-sends. β.2 only shows you what's pending; β/Phase 2 adds the
``gmail.compose`` write so "Mark sent" can actually push to Gmail Drafts.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.agent import store

router = APIRouter(tags=["agent"])
_TEMPLATE = Path(__file__).resolve().parents[1].parent / "templates" / "triage.html"


def _db_url(request: Request) -> str:
    return request.app.state.settings.database_url


# --- read --------------------------------------------------------------------


@router.get("/api/agent/pending")
def list_agent_pending(
    request: Request,
    account: str | None = Query(None),
    tier: str | None = Query(None, pattern="^(draft|surface)$"),
    status: str = Query("pending", pattern="^(pending|amended|sent|dismissed)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    rows = store.list_pending(
        _db_url(request),
        account=account,
        status=status,
        tier=tier if tier in ("draft", "surface") else None,
        limit=limit,
    )
    return {"count": len(rows), "rows": rows}


# --- state transitions -------------------------------------------------------


class AmendBody(BaseModel):
    amended_draft: str = Field(min_length=1)


@router.post("/api/agent/pending/{row_id}/amend")
def amend(row_id: int, body: AmendBody, request: Request) -> dict:
    if not store.mark_amended(_db_url(request), row_id, amended_draft=body.amended_draft):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


@router.post("/api/agent/pending/{row_id}/dismiss")
def dismiss(row_id: int, request: Request) -> dict:
    if not store.mark_dismissed(_db_url(request), row_id):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


@router.post("/api/agent/pending/{row_id}/mark_sent")
def mark_sent(row_id: int, request: Request) -> dict:
    if not store.mark_sent(_db_url(request), row_id):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


# --- triage trigger ----------------------------------------------------------


class TriageRunBody(BaseModel):
    account: str | None = None
    window: str = "7d"
    limit: int = 25
    threshold: float = 0.6
    backend: str | None = None


@router.post("/api/agent/triage")
def trigger_triage(body: TriageRunBody, request: Request) -> dict:
    """Run a fresh triage sweep (synchronous; UI shows a loading indicator).

    Per-account selection: caller passes ``account`` explicitly. β.2 just
    triages one inbox per request; γ adds the background scheduler that
    loops both configured accounts.
    """
    from app.agent.triage import run_triage
    from app.core.config import get_user_emails

    account = body.account
    if not account:
        emails = get_user_emails()
        if not emails:
            raise HTTPException(400, "no account configured (user.emails empty)")
        account = emails[0]

    settings = request.app.state.settings
    result = run_triage(
        account=account,
        window=body.window,
        limit=body.limit,
        threshold=body.threshold,
        database_url=settings.database_url,
        configs_dir=settings.configs_dir,
        backend=body.backend,
    )
    return {
        "account": account,
        "fetched": result.fetched,
        "kept": result.kept,
        "surfaced": len(result.surfaced),
        "persisted": result.persisted,
    }


# --- page --------------------------------------------------------------------


@router.get("/triage", response_class=HTMLResponse)
def triage_page() -> HTMLResponse:
    """Render the agent-triage page. Templates served per-request so live
    edits are visible without server restart (matches existing pages)."""
    if not _TEMPLATE.exists():
        raise HTTPException(500, "triage template missing")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))
