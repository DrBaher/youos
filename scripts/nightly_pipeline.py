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

from app.core.config import get_ingestion_accounts

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "var" / "youos.db"

ACCOUNTS = get_ingestion_accounts()


def _run_step(name: str, cmd: list[str], timeout: int = 600) -> bool:
    """Run a subprocess step. Returns True on success."""
    print(f"\n{'='*60}")
    print(f"STEP: {name}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
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


def step_ingest_gmail() -> bool:
    """Ingest sent emails from last 48h for all accounts."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    date_str = cutoff.strftime("%Y/%m/%d")
    query = f"in:sent after:{date_str}"

    success = True
    for account in ACCOUNTS:
        ok = _run_step(
            f"Gmail ingestion ({account})",
            [
                sys.executable,
                str(ROOT_DIR / "scripts" / "ingest_gmail_threads.py"),
                "--live",
                "--account", account,
                "--query", query,
                "--max-threads", "100",
            ],
            timeout=300,
        )
        if not ok:
            success = False
    return success


def step_auto_feedback() -> dict:
    """Extract auto-feedback pairs from last 2 days."""
    from scripts.extract_auto_feedback import extract_auto_feedback

    print(f"\n{'='*60}")
    print("STEP: Auto-feedback extraction")
    print(f"{'='*60}")
    try:
        result = extract_auto_feedback(days=2)
        print("  [OK] Auto-feedback completed")
        return result
    except Exception as exc:
        print(f"  [WARN] Auto-feedback failed: {exc}")
        return {"captured": 0, "total": 0, "skipped": 0, "errors": 0}


def step_export_feedback() -> bool:
    """Export feedback JSONL for fine-tuning."""
    return _run_step(
        "Export feedback JSONL",
        [sys.executable, str(ROOT_DIR / "scripts" / "export_feedback_jsonl.py")],
    )


def step_finetune() -> bool:
    """Run LoRA fine-tuning if enough unused pairs exist."""
    return _run_step(
        "LoRA fine-tuning",
        [sys.executable, str(ROOT_DIR / "scripts" / "finetune_lora.py")],
        timeout=3600,
    )


def step_autoresearch() -> bool:
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
        return conn.execute(
            "SELECT COUNT(*) FROM feedback_pairs WHERE used_in_finetune = 0"
        ).fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--autoresearch-only", action="store_true", help="Skip ingestion/finetune, run autoresearch only")
    args = parser.parse_args()

    if args.autoresearch_only:
        print("YouOS Autoresearch (on-demand trigger)")
        step_autoresearch()
        return

    start = datetime.now(timezone.utc)
    print(f"YouOS Nightly Pipeline — {start.isoformat()}")
    print(f"{'='*60}")

    results: dict[str, str] = {}

    # 1. Gmail ingestion
    ok = step_ingest_gmail()
    results["ingestion"] = "OK" if ok else "WARN"

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
            else:
                results["benchmark_refresh"] = "skipped (not enough new data)"
    except Exception as exc:
        results["benchmark_refresh"] = f"error: {exc}"

    # 2. Auto-feedback extraction
    feedback = step_auto_feedback()
    results["auto_feedback"] = f"captured {feedback['captured']} pairs"

    # 3. Export + fine-tune (only if enough data)
    unused = _count_unused_feedback(DEFAULT_DB)

    if feedback["captured"] >= 5:
        ok = step_export_feedback()
        results["export"] = "OK" if ok else "WARN"
    else:
        results["export"] = f"skipped (only {feedback['captured']} new pairs, need 5)"

    if unused >= 10:
        ok = step_finetune()
        results["finetune"] = "OK" if ok else "WARN"
    else:
        results["finetune"] = f"skipped (only {unused} unused pairs, need 10)"

    # 4. Autoresearch
    ok = step_autoresearch()
    results["autoresearch"] = "OK" if ok else "WARN"

    # Include recent git log after autoresearch
    try:
        git_log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10, cwd=ROOT_DIR,
        )
        if git_log.returncode == 0 and git_log.stdout.strip():
            results["recent_commits"] = git_log.stdout.strip()
    except Exception:
        pass

    # Summary
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"\n{'='*60}")
    print("NIGHTLY PIPELINE SUMMARY")
    print(f"{'='*60}")
    for step, status in results.items():
        print(f"  {step}: {status}")
    print(f"\nCompleted in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
