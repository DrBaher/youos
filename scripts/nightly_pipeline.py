"""Nightly YouOS pipeline: ingestion → auto-feedback → fine-tune → autoresearch.

Runs all steps sequentially. Each step is best-effort — failures are logged
but don't block subsequent steps.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import get_ingestion_accounts, get_last_ingest_at, set_last_ingest_at

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "var" / "youos.db"

ACCOUNTS = get_ingestion_accounts()


def _run_step(name: str, cmd: list[str], timeout: int = 600) -> bool:
    """Run a subprocess step. Returns True on success."""
    print(f"\n{'=' * 60}")
    print(f"STEP: {name}")
    print(f"{'=' * 60}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        if result.returncode != 0:
            print(f"  [WARN] {name} exited with code {result.returncode}")
            return False
        print(f"  [OK] {name} completed")
        return True
    except subprocess.TimeoutExpired:
        print(f"  [WARN] {name} timed out after {timeout}s")
        return False
    except Exception as exc:
        print(f"  [WARN] {name} failed: {exc}")
        return False


def _verbose_print(step_num: int, total: int, name: str, count: str | None = None) -> None:
    """Print Rich-style progress when verbose mode is active."""
    from rich import print as rprint

    suffix = f" done ({count})" if count else " done"
    rprint(f"[bold cyan][step {step_num}/{total}][/bold cyan] {name}...{suffix}")


def step_ingest(verbose: bool = False) -> bool:
    """Ingest sent emails incrementally for all accounts."""
    return step_ingest_gmail(verbose=verbose)


def step_ingest_gmail(verbose: bool = False) -> bool:
    """Ingest sent emails incrementally for all accounts."""
    success = True
    for account in ACCOUNTS:
        last_at = get_last_ingest_at(account)
        if last_at:
            # Incremental: use last ingestion timestamp
            date_str = last_at[:10].replace("-", "/")
            query = f"in:sent after:{date_str}"
        else:
            # Initial: use default 48h window
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            date_str = cutoff.strftime("%Y/%m/%d")
            query = f"in:sent after:{date_str}"

        ok = _run_step(
            f"Gmail ingestion ({account})",
            [
                sys.executable,
                str(ROOT_DIR / "scripts" / "ingest_gmail_threads.py"),
                "--live",
                "--account",
                account,
                "--query",
                query,
                "--max-threads",
                "100",
            ],
            timeout=300,
        )
        if ok:
            set_last_ingest_at(account, datetime.now(timezone.utc).isoformat())
        else:
            success = False
    return success


def step_analyze_persona(verbose: bool = False, dry_run: bool = False) -> bool:
    """Run persona analysis and merge results into persona.yaml."""
    # Decide whether to run --full or --recent-days
    last_full_path = ROOT_DIR / "var" / "persona_last_full_analysis.txt"
    use_full = False
    if last_full_path.exists():
        try:
            last_full_str = last_full_path.read_text(encoding="utf-8").strip()
            last_full_dt = datetime.fromisoformat(last_full_str.replace("Z", "+00:00"))
            if last_full_dt.tzinfo is None:
                last_full_dt = last_full_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_full_dt).days > 7:
                use_full = True
        except (ValueError, TypeError):
            use_full = True
    else:
        use_full = True

    cmd = [sys.executable, str(ROOT_DIR / "scripts" / "analyze_persona.py")]
    if use_full:
        cmd.append("--full")
    else:
        cmd.extend(["--recent-days", "90"])

    ok = _run_step("Persona analysis", cmd)

    # Track last full analysis
    if ok and use_full:
        last_full_path.parent.mkdir(parents=True, exist_ok=True)
        last_full_path.write_text(datetime.now(timezone.utc).isoformat())
    if not ok:
        return False

    # Merge results into persona.yaml
    try:
        from scripts.analyze_persona_merge import merge_persona_analysis

        merge_persona_analysis(
            analysis_path=ROOT_DIR / "configs" / "persona_analysis.json",
            persona_path=ROOT_DIR / "configs" / "persona.yaml",
            log_path=ROOT_DIR / "var" / "persona_merge.log",
            dry_run=dry_run,
        )
        print("  [OK] Persona merge completed")
    except Exception as exc:
        print(f"  [WARN] Persona merge failed: {exc}")
        return False
    return True


def step_build_sender_profiles(verbose: bool = False) -> bool:
    """Run sender profile builder."""
    return _run_step(
        "Build sender profiles",
        [sys.executable, str(ROOT_DIR / "scripts" / "build_sender_profiles.py")],
    )


def _load_last_auto_feedback_at() -> str | None:
    """Load last_auto_feedback_at from var/pipeline_last_run.json."""
    import json

    log_path = ROOT_DIR / "var" / "pipeline_last_run.json"
    if not log_path.exists():
        return None
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        return data.get("last_auto_feedback_at")
    except Exception:
        return None


def _save_last_auto_feedback_at() -> None:
    """Write last_auto_feedback_at to var/pipeline_last_run.json."""
    import json

    log_path = ROOT_DIR / "var" / "pipeline_last_run.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data["last_auto_feedback_at"] = datetime.now(timezone.utc).isoformat()
    log_path.write_text(json.dumps(data, indent=2))


def step_auto_feedback(verbose: bool = False) -> dict:
    """Extract auto-feedback pairs with dynamic lookback window."""
    import math

    from scripts.extract_auto_feedback import extract_auto_feedback

    print(f"\n{'=' * 60}")
    print("STEP: Auto-feedback extraction")
    print(f"{'=' * 60}")

    # Compute lookback days from last run
    last_at = _load_last_auto_feedback_at()
    if last_at:
        try:
            last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            seconds_since = (datetime.now(timezone.utc) - last_dt).total_seconds()
            days_since = max(1, math.ceil(seconds_since / 86400))
        except (ValueError, TypeError):
            days_since = 2
    else:
        days_since = 2

    print(f"  Lookback: {days_since} day(s)")

    try:
        result = extract_auto_feedback(days=days_since)
        _save_last_auto_feedback_at()
        print("  [OK] Auto-feedback completed")
        return result
    except Exception as exc:
        print(f"  [WARN] Auto-feedback failed: {exc}")
        return {"captured": 0, "total": 0, "skipped": 0, "errors": 0}


def step_export_feedback(verbose: bool = False) -> bool:
    """Export feedback JSONL for fine-tuning."""
    return _run_step(
        "Export feedback JSONL",
        [sys.executable, str(ROOT_DIR / "scripts" / "export_feedback_jsonl.py")],
    )


def step_finetune_lora(verbose: bool = False) -> bool:
    """Run LoRA fine-tuning if enough unused pairs exist."""
    return _run_step(
        "LoRA fine-tuning",
        [sys.executable, str(ROOT_DIR / "scripts" / "finetune_lora.py")],
        timeout=3600,
    )


def step_index_embeddings(verbose: bool = False) -> dict:
    """Run incremental embedding indexer."""
    result = _run_step(
        "Embedding indexer",
        [sys.executable, str(ROOT_DIR / "scripts" / "index_embeddings.py"), "--limit", "500"],
        timeout=1800,
    )
    return {"ok": result}


def step_deduplicate(verbose: bool = False) -> bool:
    """Run corpus deduplication (best-effort)."""
    return _run_step(
        "Corpus deduplication",
        [sys.executable, str(ROOT_DIR / "scripts" / "deduplicate_corpus.py")],
        timeout=300,
    )


def _check_benchmark_rotation() -> bool:
    """Check if benchmarks need rotation (> 7 days old). Returns True if rotated."""
    import json

    refresh_path = ROOT_DIR / "var" / "benchmark_last_refresh.txt"
    needs_refresh = False
    if refresh_path.exists():
        try:
            data = json.loads(refresh_path.read_text(encoding="utf-8"))
            last_dt = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_dt).days > 7:
                needs_refresh = True
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            needs_refresh = True
    else:
        needs_refresh = True

    if needs_refresh:
        now = datetime.now(timezone.utc)
        seed = hash(now.isocalendar()[:2])
        ok = _run_step(
            "Benchmark rotation",
            [sys.executable, str(ROOT_DIR / "scripts" / "generate_benchmarks.py"), "--sample-size", "30"],
        )
        if ok:
            refresh_path.parent.mkdir(parents=True, exist_ok=True)
            refresh_path.write_text(json.dumps({"timestamp": now.isoformat(), "seed": seed}))
        return ok
    return True


def _count_feedback_pairs(db_path: Path) -> int:
    """Count total feedback_pairs."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def step_golden_eval(verbose: bool = False) -> bool:
    """Run golden evaluation and return True if composite score >= 0.5."""

    # Skip if DB doesn't exist or < 5 feedback pairs
    if not DEFAULT_DB.exists():
        print("  [SKIP] golden_eval — no database")
        return True
    if _count_feedback_pairs(DEFAULT_DB) < 5:
        print("  [SKIP] golden_eval — fewer than 5 feedback pairs")
        return True

    print(f"\n{'=' * 60}")
    print("STEP: Golden evaluation")
    print(f"{'=' * 60}")

    try:
        from scripts.run_golden_eval import run_golden_eval, save_results

        summary = run_golden_eval()
        save_results(summary)

        total = summary.get("total", 0)
        passed = summary.get("passed", 0)
        composite = passed / total if total > 0 else 0.0
        print(f"  Golden eval: {passed}/{total} passed (composite: {composite:.2f})")
        print("  [OK] Golden evaluation completed")
        return composite >= 0.5
    except Exception as exc:
        print(f"  [WARN] Golden evaluation failed: {exc}")
        return False


def step_autoresearch(verbose: bool = False) -> bool:
    """Run autoresearch optimization loop."""
    # Rotate benchmarks if stale
    _check_benchmark_rotation()
    return _run_step(
        "Autoresearch",
        [sys.executable, str(ROOT_DIR / "scripts" / "run_autoresearch.py"), "--max-iter", "80"],
        timeout=7200,
    )


def _count_unused_feedback(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE used_in_finetune = 0").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _count_new_feedback_since_last_run(db_path: Path) -> int:
    """Count feedback_pairs created since last pipeline run."""
    import json

    if not db_path.exists():
        return 0
    log_path = ROOT_DIR / "var" / "pipeline_last_run.json"
    last_at = None
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            last_at = data.get("run_at")
        except Exception:
            pass

    conn = sqlite3.connect(db_path)
    try:
        if last_at:
            return conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE created_at >= ?",
                (last_at,),
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _count_null_embeddings(db_path: Path) -> int:
    """Count documents with NULL embedding."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()]
        if "embedding" not in cols:
            return -1  # no embedding column
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    except Exception:
        return -1
    finally:
        conn.close()


def _count_total_pairs(db_path: Path) -> int:
    """Count total reply_pairs."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def should_skip_finetune(db_path: Path) -> tuple[bool, str]:
    n = _count_new_feedback_since_last_run(db_path)
    if n < 3:
        return True, f"[finetune] Skipping — only {n} new pairs (need >= 3)"
    return False, ""


def should_skip_autoresearch(db_path: Path) -> tuple[bool, str]:
    n = _count_new_feedback_since_last_run(db_path)
    if n < 5:
        return True, f"[autoresearch] Skipping — only {n} new pairs (need >= 5)"
    return False, ""


def should_skip_embeddings(db_path: Path) -> tuple[bool, str]:
    n = _count_null_embeddings(db_path)
    if n == 0:
        return True, "[embeddings] Skipping — all documents already indexed"
    return False, ""


def should_skip_dedup(db_path: Path) -> tuple[bool, str]:
    n = _count_total_pairs(db_path)
    if n < 10:
        return True, f"[dedup] Skipping — corpus too small ({n} pairs)"
    return False, ""


def _write_pipeline_log(run_log: dict) -> None:
    """Write pipeline run log to var/pipeline_last_run.json."""
    import json

    log_path = ROOT_DIR / "var" / "pipeline_last_run.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(run_log, indent=2))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--autoresearch-only", action="store_true", help="Skip ingestion/finetune, run autoresearch only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print Rich progress for each step")
    args = parser.parse_args()
    verbose = args.verbose

    if args.autoresearch_only:
        print("YouOS Autoresearch (on-demand trigger)")
        step_autoresearch(verbose=verbose)
        return

    start = datetime.now(timezone.utc)
    print(f"YouOS Nightly Pipeline — {start.isoformat()}")
    print(f"{'=' * 60}")

    results: dict[str, str] = {}
    steps: dict[str, bool] = {}
    errors: list[str] = []
    skipped_steps: list[str] = []

    # 0. Corpus deduplication (best-effort, before ingestion) — with skip gate
    skip, skip_msg = should_skip_dedup(DEFAULT_DB)
    if skip:
        print(skip_msg)
        results["dedup"] = f"skipped: {skip_msg}"
        steps["dedup"] = True
        skipped_steps.append("dedup")
    else:
        try:
            ok = step_deduplicate(verbose=verbose)
            results["dedup"] = "OK" if ok else "WARN"
            steps["dedup"] = ok
            if not ok:
                errors.append("Corpus deduplication failed")
        except Exception as exc:
            results["dedup"] = f"error: {exc}"
            steps["dedup"] = False
            errors.append(f"Corpus deduplication error: {exc}")

    # 1. Gmail ingestion
    try:
        ok = step_ingest_gmail(verbose=verbose)
        results["ingestion"] = "OK" if ok else "WARN"
        steps["ingestion"] = ok
        if not ok:
            errors.append("Gmail ingestion failed")
    except Exception as exc:
        results["ingestion"] = f"error: {exc}"
        steps["ingestion"] = False
        errors.append(f"Gmail ingestion error: {exc}")

    # 1b. Benchmark auto-refresh
    try:
        from app.core.config import _load_raw_config

        cfg = _load_raw_config()
        last_count = cfg.get("benchmarks", {}).get("last_refresh_count", 0)
        if DEFAULT_DB.exists():
            conn = sqlite3.connect(DEFAULT_DB)
            current_count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
            conn.close()
            if last_count == 0 or current_count > last_count * 1.1:
                ok = _run_step(
                    "Benchmark refresh",
                    [sys.executable, str(ROOT_DIR / "scripts" / "generate_benchmarks.py")],
                )
                results["benchmark_refresh"] = "OK" if ok else "WARN"
                steps["benchmark_refresh"] = ok
                if not ok:
                    errors.append("Benchmark refresh failed")
            else:
                results["benchmark_refresh"] = "skipped (not enough new data)"
                steps["benchmark_refresh"] = True
    except Exception as exc:
        results["benchmark_refresh"] = f"error: {exc}"
        steps["benchmark_refresh"] = False
        errors.append(f"Benchmark refresh error: {exc}")

    # 2. Auto-feedback extraction
    try:
        feedback = step_auto_feedback(verbose=verbose)
        results["auto_feedback"] = f"captured {feedback['captured']} pairs"
        steps["auto_feedback"] = True
    except Exception as exc:
        feedback = {"captured": 0, "total": 0, "skipped": 0, "errors": 0}
        results["auto_feedback"] = f"error: {exc}"
        steps["auto_feedback"] = False
        errors.append(f"Auto-feedback error: {exc}")

    # 3. Export + fine-tune (only if enough data)
    unused = _count_unused_feedback(DEFAULT_DB)

    if feedback["captured"] >= 5:
        try:
            ok = step_export_feedback(verbose=verbose)
            results["export"] = "OK" if ok else "WARN"
            steps["export"] = ok
            if not ok:
                errors.append("Feedback export failed")
        except Exception as exc:
            results["export"] = f"error: {exc}"
            steps["export"] = False
            errors.append(f"Feedback export error: {exc}")
    else:
        results["export"] = f"skipped (only {feedback['captured']} new pairs, need 5)"
        steps["export"] = True

    skip_ft, skip_ft_msg = should_skip_finetune(DEFAULT_DB)
    if skip_ft:
        print(skip_ft_msg)
        results["finetune"] = f"skipped: {skip_ft_msg}"
        steps["finetune"] = True
        skipped_steps.append("finetune")
    elif unused >= 10:
        try:
            ok = step_finetune_lora(verbose=verbose)
            results["finetune"] = "OK" if ok else "WARN"
            steps["finetune"] = ok
            if not ok:
                errors.append("LoRA fine-tuning failed")
        except Exception as exc:
            results["finetune"] = f"error: {exc}"
            steps["finetune"] = False
            errors.append(f"LoRA fine-tuning error: {exc}")
    else:
        results["finetune"] = f"skipped (only {unused} unused pairs, need 10)"
        steps["finetune"] = True

    # 3b. Golden evaluation (after fine-tuning, before autoresearch)
    golden_composite = None
    try:
        ok = step_golden_eval(verbose=verbose)
        results["golden_eval"] = "OK" if ok else "WARN"
        steps["golden_eval"] = ok
        # Read composite score from results file
        golden_results_path = ROOT_DIR / "var" / "golden_results.json"
        if golden_results_path.exists():
            import json as _json2

            golden_data = _json2.loads(golden_results_path.read_text(encoding="utf-8"))
            total_g = golden_data.get("total", 0)
            passed_g = golden_data.get("passed", 0)
            golden_composite = round(passed_g / total_g, 4) if total_g > 0 else 0.0
    except Exception as exc:
        results["golden_eval"] = f"error: {exc}"
        steps["golden_eval"] = False
        errors.append(f"Golden evaluation error: {exc}")

    # 4. Embedding indexer (after fine-tuning) — with skip gate
    skip_emb, skip_emb_msg = should_skip_embeddings(DEFAULT_DB)
    if skip_emb:
        print(skip_emb_msg)
        results["embeddings"] = f"skipped: {skip_emb_msg}"
        steps["embeddings"] = True
        skipped_steps.append("embeddings")
    else:
        try:
            embed_result = step_index_embeddings(verbose=verbose)
            ok = embed_result["ok"]
            results["embeddings"] = "OK" if ok else "WARN"
            steps["embeddings"] = ok
            if not ok:
                errors.append("Embedding indexer failed")
        except Exception as exc:
            results["embeddings"] = f"error: {exc}"
            steps["embeddings"] = False
            errors.append(f"Embedding indexer error: {exc}")

    # 5. Autoresearch — with skip gate
    skip_ar, skip_ar_msg = should_skip_autoresearch(DEFAULT_DB)
    if skip_ar:
        print(skip_ar_msg)
        results["autoresearch"] = f"skipped: {skip_ar_msg}"
        steps["autoresearch"] = True
        skipped_steps.append("autoresearch")
    else:
        try:
            ok = step_autoresearch(verbose=verbose)
            results["autoresearch"] = "OK" if ok else "WARN"
            steps["autoresearch"] = ok
            if not ok:
                errors.append("Autoresearch failed")
        except Exception as exc:
            results["autoresearch"] = f"error: {exc}"
            steps["autoresearch"] = False
            errors.append(f"Autoresearch error: {exc}")

    # Include recent git log after autoresearch
    try:
        git_log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=ROOT_DIR,
        )
        if git_log.returncode == 0 and git_log.stdout.strip():
            results["recent_commits"] = git_log.stdout.strip()
    except Exception:
        pass

    # Determine overall status
    all_ok = all(steps.values())
    any_ok = any(steps.values())
    if all_ok:
        status = "ok"
    elif any_ok:
        status = "partial"
    else:
        status = "failed"

    # Write pipeline log
    # Check if benchmarks were rotated this run
    import json as _json

    benchmark_rotated = False
    refresh_path = ROOT_DIR / "var" / "benchmark_last_refresh.txt"
    if refresh_path.exists():
        try:
            rd = _json.loads(refresh_path.read_text(encoding="utf-8"))
            ref_dt = datetime.fromisoformat(rd["timestamp"].replace("Z", "+00:00"))
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)
            benchmark_rotated = (start - ref_dt).total_seconds() < 3600
        except Exception:
            pass

    run_log = {
        "run_at": start.isoformat(),
        "status": status,
        "steps": steps,
        "errors": errors,
        "skipped_steps": skipped_steps,
        "benchmark_rotated": benchmark_rotated,
        "golden_composite": golden_composite,
    }
    _write_pipeline_log(run_log)

    # Summary
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"\n{'=' * 60}")
    print("NIGHTLY PIPELINE SUMMARY")
    print(f"{'=' * 60}")
    for step, step_status in results.items():
        print(f"  {step}: {step_status}")
    print(f"\nStatus: {status}")
    print(f"Completed in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
