from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import load_config
from app.core.settings import get_var_dir
from app.core.stats import _get_adapter_path, get_corpus_stats, get_model_status, get_pipeline_status
from app.db.bootstrap import resolve_sqlite_path

router = APIRouter(tags=["stats"])

ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT_DIR / "templates" / "stats.html"


@router.get("/api/config")
def get_api_config(request: Request) -> dict[str, Any]:
    config = load_config()
    user_name = config.get("user", {}).get("name", "")
    display_name = config.get("user", {}).get("display_name", "") or "YouOS"

    db_path = resolve_sqlite_path(request.app.state.settings.database_url)
    corpus_ready = False
    model_ready = False
    feedback_pair_count = 0
    adapter_ready = (_get_adapter_path() / "adapters.safetensors").exists()
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
            corpus_ready = count > 0
            model_ready = adapter_ready
            feedback_pair_count = conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    return {
        "display_name": display_name,
        "user_name": user_name,
        "version": "0.1.10",
        "corpus_ready": corpus_ready,
        "model_ready": model_ready,
        "feedback_pair_count": feedback_pair_count,
        "adapter_ready": adapter_ready,
    }


@router.get("/stats", response_class=HTMLResponse)
def stats_page() -> HTMLResponse:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@router.get("/api/stats")
def api_stats() -> dict[str, Any]:
    """Return API-level stats including embedding cache."""
    from app.core.embeddings import get_embedding_cache_info

    return {"embedding_cache": get_embedding_cache_info()}


@router.get("/stats/data")
def stats_data(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    corpus = get_corpus_stats(settings.database_url)
    model = get_model_status(Path(settings.configs_dir))
    pipeline_last_run = get_pipeline_status(get_var_dir().parent)

    # Extract benchmark_trend from model status (kept together for source consistency)
    benchmark_trend = model.pop("benchmark_trend", [])

    # Sender profiles + cost (still needs direct DB access for row-level data)
    db_path = resolve_sqlite_path(settings.database_url)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        lora_pairs_used = 0
        try:
            row = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE used_in_finetune = 1").fetchone()
            lora_pairs_used = row[0] if row else 0
        except sqlite3.OperationalError:
            pass

        total_profiles = 0
        top_senders: list[dict[str, Any]] = []
        try:
            total_profiles = conn.execute("SELECT COUNT(*) FROM sender_profiles").fetchone()[0]
            rows = conn.execute(
                "SELECT email, display_name, company, sender_type, reply_count FROM sender_profiles ORDER BY reply_count DESC LIMIT 5"
            ).fetchall()
            for r in rows:
                top_senders.append(
                    {
                        "email": r["email"],
                        "display_name": r["display_name"],
                        "company": r["company"],
                        "sender_type": r["sender_type"],
                        "reply_count": r["reply_count"],
                    }
                )
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    total_feedback = corpus["total_feedback_pairs"]

    # Style drift detection
    drift_info: dict[str, Any] = {"status": "stable", "message": "Stable"}
    drift_path = get_var_dir() / "persona_drift.jsonl"
    if drift_path.exists():
        lines = drift_path.read_text(encoding="utf-8").strip().split("\n")
        lines = [ln for ln in lines if ln.strip()]
        if len(lines) >= 2:
            try:
                prev = json.loads(lines[-2])
                curr = json.loads(lines[-1])
                word_delta = curr.get("avg_reply_words", 0) - prev.get("avg_reply_words", 0)
                directness_delta = curr.get("directness_score", 0) - prev.get("directness_score", 0)
                if abs(word_delta) > 8 or abs(directness_delta) > 0.15:
                    drift_info["status"] = "drifting"
                    parts = []
                    if abs(word_delta) > 8:
                        direction = "shorter" if word_delta < 0 else "longer"
                        parts.append(f"replies getting {direction} ({word_delta:+.0f} words)")
                    if abs(directness_delta) > 0.15:
                        direction = "more direct" if directness_delta > 0 else "less direct"
                        parts.append(f"tone {direction}")
                    drift_info["message"] = "Drifting: " + ", ".join(parts)
            except (json.JSONDecodeError, IndexError):
                pass

    # E17: draft quality trend — weekly avg edit_distance_pct for last 8 weeks
    quality_trend: list[dict[str, Any]] = []
    # Outcome deltas: last 7d vs previous 7d
    outcome_deltas: dict[str, Any] = {}
    # E18: per-sender-type accuracy breakdown
    sender_accuracy: list[dict[str, Any]] = []
    # E19: system health card
    system_health: dict[str, Any] = {}
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    try:
        # E17: weekly quality trend
        try:
            trend_rows = conn2.execute(
                """
                SELECT
                    strftime('%Y-W%W', created_at) AS week,
                    ROUND(AVG(edit_distance_pct), 4) AS avg_edit_pct,
                    COUNT(*) AS pair_count
                FROM feedback_pairs
                WHERE edit_distance_pct IS NOT NULL
                  AND created_at >= date('now', '-56 days')
                GROUP BY week
                ORDER BY week ASC
                """
            ).fetchall()
            quality_trend = [{"week": r["week"], "avg_edit_pct": r["avg_edit_pct"], "count": r["pair_count"]} for r in trend_rows]
        except sqlite3.OperationalError:
            pass

        # Outcome deltas: recent 7d vs prior 7d
        try:
            row = conn2.execute(
                """
                SELECT
                    ROUND(
                        AVG(CASE WHEN created_at >= date('now', '-7 days') THEN edit_distance_pct END),
                        4
                    ) AS recent_edit,
                    ROUND(
                        AVG(
                            CASE
                                WHEN created_at < date('now', '-7 days')
                                 AND created_at >= date('now', '-14 days')
                                THEN edit_distance_pct
                            END
                        ),
                        4
                    ) AS prev_edit,
                    ROUND(
                        AVG(
                            CASE
                                WHEN created_at >= date('now', '-7 days')
                                THEN CASE WHEN rating >= 4 THEN 1.0 ELSE 0.0 END
                            END
                        ),
                        4
                    ) AS recent_high_rating,
                    ROUND(
                        AVG(
                            CASE
                                WHEN created_at < date('now', '-7 days')
                                 AND created_at >= date('now', '-14 days')
                                THEN CASE WHEN rating >= 4 THEN 1.0 ELSE 0.0 END
                            END
                        ),
                        4
                    ) AS prev_high_rating
                FROM feedback_pairs
                WHERE edit_distance_pct IS NOT NULL
                """
            ).fetchone()
            if row:
                recent_edit = row["recent_edit"]
                prev_edit = row["prev_edit"]
                recent_hr = row["recent_high_rating"]
                prev_hr = row["prev_high_rating"]
                outcome_deltas = {
                    "edit_distance_delta": round(recent_edit - prev_edit, 4) if recent_edit is not None and prev_edit is not None else None,
                    "high_rating_delta": round(recent_hr - prev_hr, 4) if recent_hr is not None and prev_hr is not None else None,
                    "recent_window_count": conn2.execute("SELECT COUNT(*) FROM feedback_pairs WHERE created_at >= date('now', '-7 days')").fetchone()[0],
                    "previous_window_count": conn2.execute(
                        "SELECT COUNT(*) FROM feedback_pairs WHERE created_at < date('now', '-7 days') AND created_at >= date('now', '-14 days')"
                    ).fetchone()[0],
                }
        except sqlite3.OperationalError:
            pass

        # E18: per-sender-type accuracy
        try:
            acc_rows = conn2.execute(
                """
                SELECT
                    sp.sender_type,
                    COUNT(fp.id) AS reviews,
                    ROUND(AVG(fp.edit_distance_pct), 4) AS avg_edit_pct,
                    ROUND(AVG(CAST(fp.rating AS REAL)), 2) AS avg_rating
                FROM feedback_pairs fp
                JOIN reply_pairs rp ON fp.reply_pair_id = rp.id
                JOIN sender_profiles sp ON lower(rp.inbound_author) LIKE '%' || lower(sp.email) || '%'
                WHERE fp.edit_distance_pct IS NOT NULL
                GROUP BY sp.sender_type
                ORDER BY reviews DESC
                """
            ).fetchall()
            sender_accuracy = [
                {"sender_type": r["sender_type"], "reviews": r["reviews"], "avg_edit_pct": r["avg_edit_pct"], "avg_rating": r["avg_rating"]}
                for r in acc_rows
            ]
        except sqlite3.OperationalError:
            pass

        # E19: system health
        try:
            corpus_size = conn2.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
            last_ingestion = conn2.execute("SELECT MAX(paired_at) FROM reply_pairs").fetchone()[0]
            embedding_count = conn2.execute("SELECT COUNT(*) FROM chunks WHERE metadata_json LIKE '%embedding%'").fetchone()[0]
            total_chunks = conn2.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            embedding_coverage = round(embedding_count / total_chunks, 3) if total_chunks > 0 else 0.0
            system_health = {
                "corpus_size": corpus_size,
                "last_ingestion": last_ingestion,
                "embedding_coverage": embedding_coverage,
                "adapter_ready": (_get_adapter_path() / "adapters.safetensors").exists(),
            }
        except sqlite3.OperationalError:
            pass
    finally:
        conn2.close()

    return {
        "pipeline_last_run": pipeline_last_run,
        "corpus": corpus,
        "model": {
            **model,
            "lora_pairs_used": lora_pairs_used,
        },
        "benchmark_trend": benchmark_trend,
        "senders": {
            "total_profiles": total_profiles,
            "top_senders": top_senders,
        },
        "cost": {
            "total_drafts": total_feedback,
            "local_drafts": 0,
            "claude_drafts": total_feedback,
        },
        "style_drift": drift_info,
        "quality_trend": quality_trend,
        "outcome_deltas": outcome_deltas,
        "sender_accuracy": sender_accuracy,
        "system_health": system_health,
    }
