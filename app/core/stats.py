"""Unified stats query layer for YouOS."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def _get_adapter_path() -> Path:
    from app.core.settings import get_settings

    settings = get_settings()
    if settings.data_dir is not None:
        return Path(settings.data_dir).expanduser().resolve() / "models" / "adapters" / "latest"
    return ROOT_DIR / "models" / "adapters" / "latest"


def _get_var_path(filename: str) -> Path:
    from app.core.settings import get_var_dir

    return get_var_dir() / filename


ADAPTER_PATH = _get_adapter_path()
AUTORESEARCH_JSONL = _get_var_path("autoresearch_runs.jsonl")
AUTORESEARCH_LOG = _get_var_path("autoresearch_log.md")


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


def get_corpus_stats(database_url: str) -> dict:
    """Get corpus health statistics."""
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return {
            "total_documents": 0,
            "total_reply_pairs": 0,
            "total_feedback_pairs": 0,
            "reviewed_today": 0,
            "reviewed_this_week": 0,
            "avg_edit_distance": None,
            "embedding_pct": None,
            "outcome_metrics": {
                "accept_unchanged_pct": None,
                "low_edit_pct": None,
                "high_rating_pct": None,
                "median_edit_distance": None,
            },
        }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total_docs = _safe_count(conn, "documents")
        total_pairs = _safe_count(conn, "reply_pairs")
        total_feedback = _safe_count(conn, "feedback_pairs")

        reviewed_today = 0
        reviewed_week = 0
        avg_edit_dist = None
        try:
            reviewed_today = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
            reviewed_week = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE created_at >= DATE('now', '-7 days')").fetchone()[0]
            row = conn.execute(
                "SELECT AVG(edit_distance_pct) FROM "
                "(SELECT edit_distance_pct FROM feedback_pairs "
                "WHERE edit_distance_pct IS NOT NULL ORDER BY id DESC LIMIT 50)"
            ).fetchone()
            if row and row[0] is not None:
                avg_edit_dist = round(row[0], 4)
        except sqlite3.OperationalError:
            pass

        embedding_pct = None
        try:
            if total_pairs > 0:
                with_emb = conn.execute("SELECT COUNT(*) FROM reply_pairs WHERE embedding IS NOT NULL").fetchone()[0]
                embedding_pct = round((with_emb / total_pairs) * 100, 1)
        except sqlite3.OperationalError:
            pass

        # Outcome metrics (proxy for real-world draft quality)
        outcome_metrics = {
            "accept_unchanged_pct": None,
            "low_edit_pct": None,
            "high_rating_pct": None,
            "median_edit_distance": None,
        }
        try:
            row = conn.execute(
                """
                SELECT
                    ROUND(100.0 * AVG(CASE WHEN edit_distance_pct <= 0.01 THEN 1.0 ELSE 0.0 END), 1) AS accept_unchanged_pct,
                    ROUND(100.0 * AVG(CASE WHEN edit_distance_pct <= 0.15 THEN 1.0 ELSE 0.0 END), 1) AS low_edit_pct,
                    ROUND(100.0 * AVG(CASE WHEN rating >= 4 THEN 1.0 ELSE 0.0 END), 1) AS high_rating_pct
                FROM feedback_pairs
                WHERE edit_distance_pct IS NOT NULL
                """
            ).fetchone()
            if row:
                outcome_metrics["accept_unchanged_pct"] = row[0]
                outcome_metrics["low_edit_pct"] = row[1]
                outcome_metrics["high_rating_pct"] = row[2]

            # Median edit distance from last 100 feedback rows
            med_row = conn.execute(
                """
                SELECT edit_distance_pct
                FROM feedback_pairs
                WHERE edit_distance_pct IS NOT NULL
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
            if med_row:
                vals = sorted(float(r[0]) for r in med_row)
                n = len(vals)
                if n % 2 == 1:
                    median_val = vals[n // 2]
                else:
                    median_val = (vals[(n // 2) - 1] + vals[n // 2]) / 2
                outcome_metrics["median_edit_distance"] = round(median_val, 4)
        except sqlite3.OperationalError:
            pass

        return {
            "total_documents": total_docs,
            "total_reply_pairs": total_pairs,
            "total_feedback_pairs": total_feedback,
            "reviewed_today": reviewed_today,
            "reviewed_this_week": reviewed_week,
            "avg_edit_distance": avg_edit_dist,
            "embedding_pct": embedding_pct,
            "outcome_metrics": outcome_metrics,
        }
    finally:
        conn.close()


def get_model_status(configs_dir: Path) -> dict:
    """Get model and adapter status."""
    adapter_exists = (ADAPTER_PATH / "adapters.safetensors").exists()
    lora_trained_at = None
    if adapter_exists:
        try:
            from datetime import datetime, timezone

            mtime = os.path.getmtime(ADAPTER_PATH / "adapters.safetensors")
            lora_trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except Exception:
            pass

    gen_model = "qwen2.5-1.5b-lora" if adapter_exists else "claude"

    # Benchmark trend
    benchmark_trend: list[dict] = []
    if AUTORESEARCH_JSONL.exists():
        try:
            lines = AUTORESEARCH_JSONL.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-5:]:
                entry = json.loads(line)
                benchmark_trend.append(
                    {
                        "date": entry.get("run_at", ""),
                        "composite_score": entry.get("composite_score"),
                        "improvements_kept": entry.get("config_snapshot", {}).get("improvements_kept"),
                    }
                )
        except Exception:
            benchmark_trend = []
    if not benchmark_trend and AUTORESEARCH_LOG.exists():
        try:
            import re

            log_text = AUTORESEARCH_LOG.read_text(encoding="utf-8")
            entries = re.findall(r"## Run (\d{4}-\d{2}-\d{2}[^\n]*)\n(.*?)(?=\n## Run |\Z)", log_text, re.DOTALL)
            for date_str, body in entries[-5:]:
                score_match = re.search(r"composite[_\s]?score[:\s]*([\d.]+)", body, re.IGNORECASE)
                kept_match = re.search(r"improvements?\s*kept[:\s]*(\d+)", body, re.IGNORECASE)
                benchmark_trend.append(
                    {
                        "date": date_str.strip(),
                        "composite_score": score_match.group(1) if score_match else None,
                        "improvements_kept": int(kept_match.group(1)) if kept_match else None,
                    }
                )
        except Exception:
            pass

    return {
        "generation_model": gen_model,
        "lora_adapter_exists": adapter_exists,
        "lora_trained_at": lora_trained_at,
        "last_finetune_run": lora_trained_at,
        "benchmark_trend": benchmark_trend,
    }


def get_pipeline_status(project_root: Path) -> dict | None:
    """Read var/pipeline_last_run.json if it exists."""
    log_path = project_root / "var" / "pipeline_last_run.json"
    if not log_path.exists():
        return None
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
