from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.core.diff import similarity_ratio
from app.core.sender import classify_sender
from app.core.text_utils import decode_html_entities, strip_quoted_text
from app.db.bootstrap import resolve_sqlite_path
from app.generation.service import DraftRequest, generate_draft

router = APIRouter(prefix="/review-queue", tags=["review-queue"])

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]

# Track last sender profile rebuild time (epoch seconds)
_last_sender_profile_rebuild: float = 0.0


def _trigger_sender_profile_rebuild() -> None:
    """Launch build_sender_profiles.py in the background."""
    global _last_sender_profile_rebuild
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT_DIR / "scripts" / "build_sender_profiles.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _last_sender_profile_rebuild = time.time()
        logger.info("Triggered background sender profile rebuild")
    except Exception as exc:
        logger.warning("Failed to trigger sender profile rebuild: %s", exc)


def _get_db_path(request: Request) -> Path:
    return resolve_sqlite_path(request.app.state.settings.database_url)


def _get_settings(request: Request):
    return request.app.state.settings


def score_pair_for_review(pair: dict[str, Any], reviewed_sender_types: Counter) -> float:
    """Score a candidate pair for review priority.

    Higher score = more likely to be selected.
    """
    score = 0.0

    # Recency bonus — prefer pairs from last 6 months
    six_months_ago = datetime.now(tz=timezone.utc) - timedelta(days=180)
    paired_at = pair.get("paired_at") or ""
    if paired_at:
        try:
            dt = datetime.fromisoformat(paired_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > six_months_ago:
                score += 0.3
        except (ValueError, TypeError):
            pass

    # Sender type diversity bonus
    sender_type = classify_sender(pair.get("inbound_author"))
    if reviewed_sender_types[sender_type] < 2:
        score += 0.4

    # Length filter — prefer medium length (100-500 chars inbound)
    inbound_len = len(pair.get("inbound_text") or "")
    if 100 < inbound_len < 500:
        score += 0.3

    return score


def _fetch_candidates(
    db_path: Path,
    batch_size: int,
    exclude_ids: list[int],
) -> tuple[list[dict[str, Any]], int]:
    """Select reply_pairs not yet reviewed, with smart scoring for diversity."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Count total unreviewed
        total_unreviewed = conn.execute(
            """
            SELECT COUNT(*) FROM reply_pairs rp
            WHERE NOT EXISTS (
                SELECT 1 FROM feedback_pairs fp WHERE fp.reply_pair_id = rp.id
            )
            """
        ).fetchone()[0]

        # Build exclude clause
        placeholders = ""
        params: list[Any] = []
        if exclude_ids:
            placeholders = " AND rp.id NOT IN ({})".format(
                ",".join("?" for _ in exclude_ids)
            )
            params.extend(exclude_ids)

        # Fetch a larger pool for scoring
        query = """
            SELECT rp.id, rp.inbound_text, rp.inbound_author, rp.reply_text,
                   rp.paired_at, rp.metadata_json,
                   d.title as doc_title,
                   d.metadata_json as doc_metadata_json
            FROM reply_pairs rp
            LEFT JOIN documents d ON rp.document_id = d.id
            WHERE NOT EXISTS (
                SELECT 1 FROM feedback_pairs fp WHERE fp.reply_pair_id = rp.id
            )
            AND LENGTH(rp.inbound_text) >= 50
            AND rp.inbound_text NOT LIKE '---------- Forwarded%%'
            {}
            ORDER BY RANDOM()
            LIMIT ?
        """.format(placeholders)
        params.append(batch_size * 5)  # fetch larger pool for scoring

        rows = conn.execute(query, params).fetchall()

        # First pass: filter out automated senders, short replies, and build candidate list
        pool: list[dict[str, Any]] = []
        for row in rows:
            author = row["inbound_author"] or ""
            author_lower = author.lower()
            if any(
                prefix in author_lower
                for prefix in [
                    "no-reply", "noreply", "donotreply", "do-not-reply",
                    "mailer-daemon", "notifications",
                ]
            ):
                continue

            # Skip very short replies (< 20 chars) — not useful training signal
            if len(row["reply_text"] or "") < 20:
                continue

            doc_meta = {}
            if row["doc_metadata_json"]:
                try:
                    doc_meta = json.loads(row["doc_metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            pool.append({
                "reply_pair_id": row["id"],
                "inbound_text": row["inbound_text"],
                "inbound_author": row["inbound_author"],
                "subject": row["doc_title"],
                "reply_text": row["reply_text"],
                "paired_at": row["paired_at"],
                "account_email": doc_meta.get("account_email"),
            })

        # Second pass: score and select with diversity
        reviewed_sender_types: Counter = Counter()
        scored = [
            (score_pair_for_review(p, reviewed_sender_types), i, p) for i, p in enumerate(pool)
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        candidates: list[dict[str, Any]] = []
        for _score, _idx, pair in scored:
            if len(candidates) >= batch_size:
                break
            sender_type = classify_sender(pair.get("inbound_author"))
            candidates.append(pair)
            reviewed_sender_types[sender_type] += 1

        return candidates, total_unreviewed
    finally:
        conn.close()


def _lookup_sender_profile_safe(
    db_path: Path, email: str
) -> dict[str, Any] | None:
    """Look up sender profile, returning None if table doesn't exist or no match."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sender_profiles'"
        ).fetchone()
        if not exists:
            return None
        # Extract email address from "Name <email>" format
        import re
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", email)
        if not match:
            return None
        clean_email = match.group(0).lower()
        row = conn.execute(
            "SELECT * FROM sender_profiles WHERE email = ?", (clean_email,)
        ).fetchone()
        if not row:
            return None
        return {
            "display_name": row["display_name"],
            "company": row["company"],
            "sender_type": row["sender_type"],
            "reply_count": row["reply_count"],
        }
    except Exception:
        return None
    finally:
        conn.close()


def _count_reviewed_today(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


@router.get("/next")
def review_queue_next(
    request: Request,
    batch_size: int = Query(default=10, ge=1, le=50),
    exclude_ids: str = Query(default=""),
) -> dict:
    db_path = _get_db_path(request)
    settings = _get_settings(request)

    # Parse exclude_ids
    excluded: list[int] = []
    if exclude_ids.strip():
        excluded = [int(x) for x in exclude_ids.split(",") if x.strip().isdigit()]

    candidates, total_unreviewed = _fetch_candidates(db_path, batch_size, excluded)

    # Generate drafts for each candidate
    items = []
    for cand in candidates:
        # Decode HTML entities and strip quoted text before display and generation
        clean_inbound = strip_quoted_text(decode_html_entities(cand["inbound_text"]))
        try:
            draft_response = generate_draft(
                DraftRequest(
                    inbound_message=clean_inbound,
                    sender=cand["inbound_author"],
                ),
                database_url=settings.database_url,
                configs_dir=settings.configs_dir,
            )
            generated_draft = draft_response.draft
        except Exception as exc:
            logger.warning("Draft generation failed for rp %s: %s", cand["reply_pair_id"], exc)
            continue

        # Look up sender profile
        sender_profile = None
        if cand["inbound_author"]:
            sender_profile = _lookup_sender_profile_safe(db_path, cand["inbound_author"])

        items.append({
            "reply_pair_id": cand["reply_pair_id"],
            "inbound_text": clean_inbound,
            "inbound_author": cand["inbound_author"],
            "subject": cand["subject"],
            "generated_draft": generated_draft,
            "sender_profile": sender_profile,
            "account_email": cand.get("account_email"),
            "paired_at": cand["paired_at"],
        })

        if len(items) >= batch_size:
            break

    # Trigger sender profile rebuild if last rebuild was > 1 hour ago
    global _last_sender_profile_rebuild
    if items and (time.time() - _last_sender_profile_rebuild) > 3600:
        _trigger_sender_profile_rebuild()

    reviewed_today = _count_reviewed_today(db_path)

    return {
        "items": items,
        "total_unreviewed": total_unreviewed,
        "reviewed_today": reviewed_today,
    }


class ReviewSubmitBody(BaseModel):
    reply_pair_id: int
    inbound_text: str = Field(min_length=1)
    generated_draft: str = Field(min_length=1)
    edited_reply: str = Field(min_length=1)
    feedback_note: str | None = None
    rating: int = Field(default=4, ge=1, le=5)


@router.post("/submit")
def review_queue_submit(body: ReviewSubmitBody, request: Request) -> dict:
    db_path = _get_db_path(request)
    edit_distance_pct = round(
        1.0 - similarity_ratio(body.generated_draft, body.edited_reply), 4
    )

    conn = sqlite3.connect(db_path)
    try:
        # Check for duplicate submission
        existing = conn.execute(
            "SELECT id FROM feedback_pairs WHERE reply_pair_id = ?",
            (body.reply_pair_id,),
        ).fetchone()
        if existing:
            return {"status": "already_submitted", "feedback_id": existing[0]}

        conn.execute(
            """
            INSERT INTO feedback_pairs
                (inbound_text, generated_draft, edited_reply, feedback_note,
                 rating, edit_distance_pct, reply_pair_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.inbound_text,
                body.generated_draft,
                body.edited_reply,
                body.feedback_note,
                body.rating,
                edit_distance_pct,
                body.reply_pair_id,
            ),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
    finally:
        conn.close()

    # Trigger sender profile rebuild every 10 submissions
    if total % 10 == 0:
        _trigger_sender_profile_rebuild()

    return {"status": "saved", "total_pairs": total, "edit_distance_pct": edit_distance_pct}


@router.post("/trigger-autoresearch")
def trigger_autoresearch() -> dict:
    """Fire off the autoresearch loop in the background after a batch is complete."""
    import subprocess
    import threading

    def _run() -> None:
        try:
            venv_python = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python3"
            script = Path(__file__).resolve().parents[3] / "scripts" / "nightly_pipeline.py"
            subprocess.run([str(venv_python), str(script), "--autoresearch-only"], timeout=7200)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started", "message": "Autoresearch optimization started in background."}


class CompareBody(BaseModel):
    inbound_text: str = Field(min_length=1)
    sender: str | None = None


@router.post("/compare")
def draft_compare(body: CompareBody, request: Request) -> dict:
    """Generate two drafts: retrieval-grounded vs baseline (persona-only)."""
    settings = _get_settings(request)
    clean_inbound = strip_quoted_text(body.inbound_text)

    # Full retrieval-grounded draft
    try:
        retrieval_response = generate_draft(
            DraftRequest(
                inbound_message=clean_inbound,
                sender=body.sender,
            ),
            database_url=settings.database_url,
            configs_dir=settings.configs_dir,
        )
        retrieval_draft = retrieval_response.draft
        retrieval_confidence = retrieval_response.confidence
        exemplar_count = len(retrieval_response.precedent_used)
    except Exception as exc:
        retrieval_draft = f"[generation failed: {exc}]"
        retrieval_confidence = "error"
        exemplar_count = 0

    # Baseline draft (no exemplars — just persona + inbound)
    try:
        baseline_response = generate_draft(
            DraftRequest(
                inbound_message=clean_inbound,
                sender=body.sender,
                top_k_reply_pairs=0,
                top_k_chunks=0,
            ),
            database_url=settings.database_url,
            configs_dir=settings.configs_dir,
        )
        baseline_draft = baseline_response.draft
    except Exception as exc:
        baseline_draft = f"[generation failed: {exc}]"

    return {
        "retrieval_draft": retrieval_draft,
        "baseline_draft": baseline_draft,
        "retrieval_confidence": retrieval_confidence,
        "exemplar_count": exemplar_count,
    }
