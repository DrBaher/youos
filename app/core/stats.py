"""Unified stats query layer for YouOS."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def _get_var_path(filename: str) -> Path:
    from app.core.settings import get_var_dir

    return get_var_dir() / filename


def _resolve_adapter_path() -> Path:
    from app.core.settings import get_adapter_path

    return get_adapter_path()


ADAPTER_PATH = _resolve_adapter_path()
AUTORESEARCH_JSONL = _get_var_path("autoresearch_runs.jsonl")
AUTORESEARCH_LOG = _get_var_path("autoresearch_log.md")


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


def _group_counts(conn: sqlite3.Connection, column: str, *, default: str) -> dict[str, int]:
    """COUNT(*) grouped by a draft_events column (NULLs folded to *default*)."""
    try:
        rows = conn.execute(
            f"SELECT COALESCE({column}, ?) AS k, COUNT(*) AS n FROM draft_events GROUP BY k ORDER BY n DESC",  # noqa: S608
            (default,),
        ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}
    except sqlite3.OperationalError:
        return {}


def summarize_draft_events(database_url: str) -> dict:
    """Aggregate the ``draft_events`` signal log into a draft-quality picture.

    Every generated draft is logged with the *conditions* it was produced
    under (intent, sender_type, confidence, length_flag). This summarizes them
    so the loop can see *where* drafting is weak — e.g. an intent whose drafts
    are frequently off the target length, or a cohort that draws mostly
    low-confidence retrieval. Where a draft can be matched to an edit outcome
    (a best-effort join to ``draft_history`` on inbound+draft text), it also
    reports the average edit distance by condition. The model's own drafts are
    never training targets; this is analysis/observability for the loop.
    """
    from app.db.bootstrap import resolve_sqlite_path

    empty = {
        "total": 0,
        "by_intent": {},
        "by_sender_type": {},
        "by_confidence": {},
        "by_length_flag": {},
        "off_target_pct": None,
        "outcome": {"matched": 0, "avg_edit_distance_by_sender_type": {}, "avg_edit_distance_by_confidence": {}},
    }

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return empty

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = _safe_count(conn, "draft_events")
        if total == 0:
            return empty

        summary = {
            "total": total,
            "by_intent": _group_counts(conn, "intent", default="unknown"),
            "by_sender_type": _group_counts(conn, "sender_type", default="unknown"),
            "by_confidence": _group_counts(conn, "confidence", default="unknown"),
            "by_length_flag": _group_counts(conn, "length_flag", default="none"),
            "off_target_pct": None,
            "outcome": {"matched": 0, "avg_edit_distance_by_sender_type": {}, "avg_edit_distance_by_confidence": {}},
        }

        # Fraction of length-annotated drafts that missed the target band.
        try:
            off, flagged = conn.execute(
                """SELECT
                       SUM(CASE WHEN length_flag IN ('long', 'short') THEN 1 ELSE 0 END),
                       SUM(CASE WHEN length_flag IS NOT NULL THEN 1 ELSE 0 END)
                   FROM draft_events"""
            ).fetchone()
            if flagged:
                summary["off_target_pct"] = round(100.0 * (off or 0) / flagged, 1)
        except sqlite3.OperationalError:
            pass

        # Best-effort outcome correlation: join to draft_history on the only
        # available linkage (inbound + draft text). Not unique — same draft can
        # recur — so this is indicative, not exact; `matched` reports coverage.
        for key, col in (("avg_edit_distance_by_sender_type", "sender_type"), ("avg_edit_distance_by_confidence", "confidence")):
            try:
                rows = conn.execute(
                    f"""SELECT COALESCE(de.{col}, 'unknown') AS k,
                               ROUND(AVG(dh.edit_distance_pct), 3) AS avg_ed,
                               COUNT(*) AS n
                        FROM draft_events de
                        JOIN draft_history dh
                          ON de.inbound_text = dh.inbound_text
                         AND de.generated_draft = dh.generated_draft
                        WHERE dh.edit_distance_pct IS NOT NULL
                        GROUP BY k""",  # noqa: S608
                ).fetchall()
                summary["outcome"][key] = {str(r["k"]): {"avg_edit_distance": r["avg_ed"], "n": int(r["n"])} for r in rows}
            except sqlite3.OperationalError:
                pass

        try:
            matched = conn.execute(
                """SELECT COUNT(*) FROM draft_events de
                   JOIN draft_history dh
                     ON de.inbound_text = dh.inbound_text AND de.generated_draft = dh.generated_draft
                   WHERE dh.edit_distance_pct IS NOT NULL"""
            ).fetchone()[0]
            summary["outcome"]["matched"] = int(matched)
        except sqlite3.OperationalError:
            pass

        return summary
    finally:
        conn.close()


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


def get_persona_adapter_status() -> dict[str, dict]:
    """Return ``{persona: {trained: bool, mtime: iso|None, pairs_used: int|None}}``.

    One entry per sender_type cohort (internal / external_client / personal /
    automated) — "unknown" excluded since Phase 2 doesn't train an adapter
    for it. Used by the stats endpoint and the doctor check so the user
    can see which personas have a trained adapter without poking the
    filesystem.

    ``mtime`` and ``pairs_used`` come from the adapter's `meta.json`
    (written by `scripts/finetune_lora.py`) when present; otherwise mtime
    falls back to the safetensors file's mtime and pairs_used is None.
    """
    from app.core.settings import get_persona_adapter_path

    out: dict[str, dict] = {}
    for persona in ("internal", "external_client", "personal", "automated"):
        adapter_dir = get_persona_adapter_path(persona)
        sfile = adapter_dir / "adapters.safetensors"
        meta_path = adapter_dir / "meta.json"
        entry: dict = {"trained": sfile.exists(), "mtime": None, "pairs_used": None}
        if not sfile.exists():
            out[persona] = entry
            continue
        # Prefer the train metadata json; fall back to fs mtime.
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry["mtime"] = meta.get("trained_at")
                entry["pairs_used"] = meta.get("pairs_used")
            except Exception:
                pass
        if entry["mtime"] is None:
            try:
                from datetime import datetime, timezone

                entry["mtime"] = datetime.fromtimestamp(
                    sfile.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        out[persona] = entry
    return out


def get_embedding_coverage(database_url: str) -> dict[str, float]:
    """Fraction of rows with non-empty embeddings, per indexed table.

    Returns ``{"chunks": 0.42, "reply_pairs": 0.31}`` when both tables exist
    and have rows. Missing / empty tables are silently omitted — so a fresh
    instance returns ``{}`` rather than zeros that would imply "indexed,
    but every row failed".

    Used to answer "is semantic retrieval actually firing on this corpus?"
    from the stats endpoint and the nightly pipeline log — without this,
    the only way to tell was to add ad-hoc logging inside the retrieval
    reranker. Mirrors ``app.retrieval.service._embedding_coverage`` which
    is per-table and connection-scoped; this is the public, multi-table,
    db-path-scoped version intended for stats callers.
    """
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return {}

    coverage: dict[str, float] = {}
    conn = sqlite3.connect(db_path)
    try:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in ("chunks", "reply_pairs"):
            if table not in existing_tables:
                continue
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "embedding" not in cols:
                continue
            total = _safe_count(conn, table)
            if total == 0:
                continue
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE embedding IS NOT NULL AND LENGTH(embedding) > 0"  # noqa: S608
            ).fetchone()
            embedded = row[0] if row else 0
            coverage[table] = round(embedded / total, 4)
    except sqlite3.OperationalError:
        return coverage
    finally:
        conn.close()
    return coverage
