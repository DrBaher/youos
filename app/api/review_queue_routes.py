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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_review_batch_size
from app.core.diff import hybrid_similarity, similarity_ratio
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

    # Continuous recency score — 1.0 for today, 0.0 for 1yr+
    if paired_at:
        try:
            dt = datetime.fromisoformat(paired_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_old = (datetime.now(tz=timezone.utc) - dt).days
            recency_score = max(0.0, 1.0 - (days_old / 365))
            score += recency_score * 0.3
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
            placeholders = " AND rp.id NOT IN ({})".format(",".join("?" for _ in exclude_ids))
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
            ORDER BY rp.paired_at DESC
            LIMIT ?
        """.format(placeholders)
        params.append(batch_size * 5)  # fetch larger pool for scoring

        rows = conn.execute(query, params).fetchall()

        # First pass: filter out automated senders, short replies, and build candidate list
        pool: list[dict[str, Any]] = []
        for row in rows:
            author = row["inbound_author"] or ""
            author_lower = author.lower()

            # Hard filter: automated sender prefixes
            if any(
                prefix in author_lower
                for prefix in [
                    "no-reply",
                    "noreply",
                    "donotreply",
                    "do-not-reply",
                    "mailer-daemon",
                    "notifications",
                    "notification",
                    "receipt",
                    "invoice",
                    "billing",
                    "payment",
                    "confirm",
                    "automated",
                    "newsletter",
                    "marketing",
                    "support@",
                    "info@",
                    "hello@",
                    "team@",
                    "contact@",
                ]
            ):
                continue

            # Also use classify_sender to catch automated senders not covered by prefixes
            sender_type = classify_sender(author)
            if sender_type == "automated":
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

            pool.append(
                {
                    "reply_pair_id": row["id"],
                    "inbound_text": row["inbound_text"],
                    "inbound_author": row["inbound_author"],
                    "subject": row["doc_title"],
                    "reply_text": row["reply_text"],
                    "paired_at": row["paired_at"],
                    "account_email": doc_meta.get("account_email"),
                }
            )

        # Second pass: score and select with diversity
        reviewed_sender_types: Counter = Counter()
        scored = [(score_pair_for_review(p, reviewed_sender_types), i, p) for i, p in enumerate(pool)]
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


def _lookup_sender_profile_safe(db_path: Path, email: str) -> dict[str, Any] | None:
    """Look up sender profile, returning None if table doesn't exist or no match."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sender_profiles'").fetchone()
        if not exists:
            return None
        # Extract email address from "Name <email>" format
        import re

        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", email)
        if not match:
            return None
        clean_email = match.group(0).lower()
        row = conn.execute("SELECT * FROM sender_profiles WHERE email = ?", (clean_email,)).fetchone()
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
        row = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


@router.get("/next")
def review_queue_next(
    request: Request,
    batch_size: int = Query(default=None, ge=1, le=50),
    exclude_ids: str = Query(default=""),
) -> dict:
    db_path = _get_db_path(request)
    settings = _get_settings(request)

    # Use config batch_size if not explicitly provided
    if batch_size is None:
        batch_size = get_review_batch_size()

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

        items.append(
            {
                "reply_pair_id": cand["reply_pair_id"],
                "inbound_text": clean_inbound,
                "inbound_author": cand["inbound_author"],
                "subject": cand["subject"],
                "generated_draft": generated_draft,
                "sender_profile": sender_profile,
                "account_email": cand.get("account_email"),
                "paired_at": cand["paired_at"],
                "suggested_subject": getattr(draft_response, "suggested_subject", None),
            }
        )

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


@router.get("/next-stream")
def review_queue_next_stream(
    request: Request,
    batch_size: int = Query(default=None, ge=1, le=50),
    exclude_ids: str = Query(default=""),
) -> StreamingResponse:
    """Stream review queue items one by one as SSE, generating drafts progressively."""
    db_path = _get_db_path(request)
    settings = _get_settings(request)

    if batch_size is None:
        batch_size = get_review_batch_size()

    excluded: list[int] = []
    if exclude_ids.strip():
        excluded = [int(x) for x in exclude_ids.split(",") if x.strip().isdigit()]

    candidates, total_unreviewed = _fetch_candidates(db_path, batch_size, excluded)
    reviewed_today = _count_reviewed_today(db_path)

    def _generate() -> Any:
        # Send metadata first
        meta = json.dumps({
            "type": "meta",
            "total_unreviewed": total_unreviewed,
            "reviewed_today": reviewed_today,
            "batch_size": len(candidates),
        })
        yield f"data: {meta}\n\n"

        item_count = 0
        for cand in candidates:
            if item_count >= batch_size:
                break
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

            sender_profile = None
            if cand["inbound_author"]:
                sender_profile = _lookup_sender_profile_safe(db_path, cand["inbound_author"])

            item = {
                "type": "item",
                "reply_pair_id": cand["reply_pair_id"],
                "inbound_text": clean_inbound,
                "inbound_author": cand["inbound_author"],
                "subject": cand["subject"],
                "generated_draft": generated_draft,
                "sender_profile": sender_profile,
                "account_email": cand.get("account_email"),
                "paired_at": cand["paired_at"],
                "suggested_subject": getattr(draft_response, "suggested_subject", None),
            }
            yield f"data: {json.dumps(item)}\n\n"
            item_count += 1

        yield "data: {\"type\": \"done\"}\n\n"

        global _last_sender_profile_rebuild
        if item_count > 0 and (time.time() - _last_sender_profile_rebuild) > 3600:
            _trigger_sender_profile_rebuild()

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/submit")
def review_queue_submit(body: ReviewSubmitBody, request: Request) -> dict:
    db_path = _get_db_path(request)
    edit_distance_pct = round(1.0 - similarity_ratio(body.generated_draft, body.edited_reply), 4)

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

        # Save to draft_history
        try:
            conn.execute(
                """INSERT INTO draft_history
                   (inbound_text, sender, generated_draft, final_reply, edit_distance_pct)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.inbound_text, None, body.generated_draft, body.edited_reply, edit_distance_pct),
            )
            conn.commit()
        except Exception:
            pass
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
    """Compare Qwen+LoRA adapter vs Qwen base (no adapter)."""
    settings = _get_settings(request)
    clean_inbound = strip_quoted_text(body.inbound_text)

    # Adapter draft (with LoRA adapter + exemplars)
    try:
        adapter_response = generate_draft(
            DraftRequest(
                inbound_message=clean_inbound,
                sender=body.sender,
                use_adapter=True,
            ),
            database_url=settings.database_url,
            configs_dir=settings.configs_dir,
        )
        adapter_draft = adapter_response.draft
        adapter_confidence = adapter_response.confidence
        exemplar_count = len(adapter_response.precedent_used)
    except Exception as exc:
        adapter_draft = f"[generation failed: {exc}]"
        adapter_confidence = "error"
        exemplar_count = 0

    # Base draft (no adapter, no exemplars)
    try:
        base_response = generate_draft(
            DraftRequest(
                inbound_message=clean_inbound,
                sender=body.sender,
                use_adapter=False,
                top_k_reply_pairs=0,
                top_k_chunks=0,
            ),
            database_url=settings.database_url,
            configs_dir=settings.configs_dir,
        )
        base_draft = base_response.draft
    except Exception as exc:
        base_draft = f"[generation failed: {exc}]"

    # Compute improvement hint based on similarity
    try:
        sim = hybrid_similarity(adapter_draft, base_draft)
        if sim < 0.7:
            improvement_hint = "Adapter appears to be helping"
        else:
            improvement_hint = "Drafts similar — adapter may need more training"
    except Exception:
        improvement_hint = "Unable to compare drafts"

    return {
        "adapter_draft": adapter_draft,
        "base_draft": base_draft,
        "adapter_confidence": adapter_confidence,
        "exemplar_count": exemplar_count,
        "improvement_hint": improvement_hint,
    }
