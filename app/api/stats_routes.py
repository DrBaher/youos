from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import load_config
from app.db.bootstrap import resolve_sqlite_path

router = APIRouter(tags=["stats"])

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "stats.html"
ADAPTER_PATH = Path(__file__).resolve().parents[2] / "models" / "adapters" / "latest"
AUTORESEARCH_LOG = Path(__file__).resolve().parents[2] / "autoresearch_log.md"
AUTORESEARCH_JSONL = Path(__file__).resolve().parents[2] / "var" / "autoresearch_runs.jsonl"


@router.get("/api/config")
def get_api_config(request: Request) -> dict[str, Any]:
    config = load_config()
    user_name = config.get("user", {}).get("name", "")
    display_name = f"{user_name}OS" if user_name else "YouOS"

    db_path = resolve_sqlite_path(request.app.state.settings.database_url)
    corpus_ready = False
    model_ready = False
    feedback_pair_count = 0
    adapter_ready = (ADAPTER_PATH / "adapters.safetensors").exists()
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
        "version": "0.1.0",
        "corpus_ready": corpus_ready,
        "model_ready": model_ready,
        "feedback_pair_count": feedback_pair_count,
        "adapter_ready": adapter_ready,
    }


def _get_db_path(request: Request) -> Path:
    return resolve_sqlite_path(request.app.state.settings.database_url)


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


@router.get("/stats", response_class=HTMLResponse)
def stats_page() -> HTMLResponse:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@router.get("/stats/data")
def stats_data(request: Request) -> dict[str, Any]:
    db_path = _get_db_path(request)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Corpus health
        total_docs = _safe_count(conn, "documents")
        total_pairs = _safe_count(conn, "reply_pairs")
        total_feedback = _safe_count(conn, "feedback_pairs")

        reviewed_today = 0
        reviewed_week = 0
        avg_edit_dist = None
        try:
            reviewed_today = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')"
            ).fetchone()[0]
            reviewed_week = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE created_at >= DATE('now', '-7 days')"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT AVG(edit_distance_pct) FROM "
                "(SELECT edit_distance_pct FROM feedback_pairs "
                "WHERE edit_distance_pct IS NOT NULL ORDER BY id DESC LIMIT 50)"
            ).fetchone()
            if row and row[0] is not None:
                avg_edit_dist = round(row[0], 4)
        except sqlite3.OperationalError:
            pass

        # Embedding percentage
        embedding_pct = None
        try:
            total_rp = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
            if total_rp > 0:
                with_emb = conn.execute(
                    "SELECT COUNT(*) FROM reply_pairs WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                embedding_pct = round((with_emb / total_rp) * 100, 1)
        except sqlite3.OperationalError:
            pass

        # Model status
        adapter_exists = (ADAPTER_PATH / "adapters.safetensors").exists()
        lora_trained_at = None
        if adapter_exists:
            try:
                import os
                mtime = os.path.getmtime(ADAPTER_PATH / "adapters.safetensors")
                from datetime import datetime, timezone
                lora_trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except Exception:
                pass

        lora_pairs_used = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE used_in_finetune = 1"
            ).fetchone()
            lora_pairs_used = row[0] if row else 0
        except sqlite3.OperationalError:
            pass

        gen_model = "qwen2.5-1.5b-lora" if adapter_exists else "claude"

        # Benchmark trend (last 5 entries from JSONL, fallback to markdown)
        benchmark_trend: list[dict[str, Any]] = []
        if AUTORESEARCH_JSONL.exists():
            try:
                import json as _json
                lines = AUTORESEARCH_JSONL.read_text(encoding="utf-8").strip().splitlines()
                for line in lines[-5:]:
                    entry = _json.loads(line)
                    benchmark_trend.append({
                        "date": entry.get("run_at", ""),
                        "composite_score": entry.get("composite_score"),
                        "improvements_kept": entry.get("config_snapshot", {}).get("improvements_kept"),
                    })
            except Exception:
                benchmark_trend = []
        if not benchmark_trend and AUTORESEARCH_LOG.exists():
            try:
                import re
                log_text = AUTORESEARCH_LOG.read_text(encoding="utf-8")
                entries = re.findall(
                    r"## Run (\d{4}-\d{2}-\d{2}[^\n]*)\n(.*?)(?=\n## Run |\Z)",
                    log_text, re.DOTALL
                )
                for date_str, body in entries[-5:]:
                    score_match = re.search(
                        r"composite[_\s]?score[:\s]*([\d.]+)", body, re.IGNORECASE
                    )
                    kept_match = re.search(r"improvements?\s*kept[:\s]*(\d+)", body, re.IGNORECASE)
                    benchmark_trend.append({
                        "date": date_str.strip(),
                        "composite_score": score_match.group(1) if score_match else None,
                        "improvements_kept": int(kept_match.group(1)) if kept_match else None,
                    })
            except Exception:
                pass

        # Sender profiles
        total_profiles = 0
        top_senders: list[dict[str, Any]] = []
        try:
            total_profiles = _safe_count(conn, "sender_profiles")
            rows = conn.execute(
                "SELECT email, display_name, company, sender_type, reply_count "
                "FROM sender_profiles ORDER BY reply_count DESC LIMIT 5"
            ).fetchall()
            for r in rows:
                top_senders.append({
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "company": r["company"],
                    "sender_type": r["sender_type"],
                    "reply_count": r["reply_count"],
                })
        except sqlite3.OperationalError:
            pass

        # Cost savings (feedback_pairs as proxy for drafts)
        total_drafts = total_feedback
        local_drafts = 0
        claude_drafts = total_feedback

        return {
            "corpus": {
                "total_documents": total_docs,
                "total_reply_pairs": total_pairs,
                "total_feedback_pairs": total_feedback,
                "reviewed_today": reviewed_today,
                "reviewed_this_week": reviewed_week,
                "avg_edit_distance": avg_edit_dist,
                "embedding_pct": embedding_pct,
            },
            "model": {
                "generation_model": gen_model,
                "lora_adapter_exists": adapter_exists,
                "lora_trained_at": lora_trained_at,
                "lora_pairs_used": lora_pairs_used,
                "last_finetune_run": lora_trained_at,
            },
            "benchmark_trend": benchmark_trend,
            "senders": {
                "total_profiles": total_profiles,
                "top_senders": top_senders,
            },
            "cost": {
                "total_drafts": total_drafts,
                "local_drafts": local_drafts,
                "claude_drafts": claude_drafts,
            },
        }
    finally:
        conn.close()
