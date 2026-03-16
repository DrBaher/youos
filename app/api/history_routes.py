from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Query, Request

from app.db.bootstrap import resolve_sqlite_path

router = APIRouter(prefix="/history", tags=["history"])


def _get_db_path(request: Request) -> Path:
    return resolve_sqlite_path(request.app.state.settings.database_url)


@router.get("")
def get_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    db_path = _get_db_path(request)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Check if table exists
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_history'").fetchone()
        if not exists:
            return {"items": [], "total": 0}
        rows = conn.execute(
            """SELECT id, inbound_text, sender, generated_draft, final_reply,
                      edit_distance_pct, confidence, model_used, retrieval_method,
                      created_at
               FROM draft_history
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM draft_history").fetchone()[0]
        items = [dict(row) for row in rows]
        return {"items": items, "total": total}
    finally:
        conn.close()
