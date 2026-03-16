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
    # Run analysis
    ok = _run_step(
        "Persona analysis",
        [sys.executable, str(ROOT_DIR / "scripts" / "analyze_persona.py")],
    )
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


def step_auto_feedback(verbose: bool = False) -> dict:
    """Extract auto-feedback pairs from last 2 days."""
    from scripts.extract_auto_feedback import extract_auto_feedback

    print(f"\n{'=' * 60}")
    print("STEP: Auto-feedback extraction")
    print(f"{'=' * 60}")
    try:
        result = extract_auto_feedback(days=2)
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


def step_autoresearch(verbose: bool = False) -> bool:
    """Run autoresearch optimization loop."""
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

    # 0. Corpus deduplication (best-effort, before ingestion)
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

    if unused >= 10:
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

    # 4. Embedding indexer (after fine-tuning)
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

    # 5. Autoresearch
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
    run_log = {
        "run_at": start.isoformat(),
        "status": status,
        "steps": steps,
        "errors": errors,
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
