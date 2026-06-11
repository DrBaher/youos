"""Inbox-replay backtest CLI (b234).

Replays the N most recent real reply pairs through the current drafting
pipeline (answers held out of retrieval) and diffs each draft against the
user's real reply. Writes a JSON report into the instance var/ and prints a
summary.

    YOUOS_DATA_DIR=... python scripts/replay_backtest.py --n 80

Deterministic + local-only: a re-run with the same corpus reproduces the same
scorecard, and no cloud calls are made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical inbounds; compare drafts to the user's real replies.")
    parser.add_argument("--n", type=int, default=80, help="number of recent reply pairs to replay")
    parser.add_argument("--out", type=Path, default=None, help="output JSON path (default: <var>/replay_backtest.json)")
    parser.add_argument("--no-semantic", action="store_true", help="skip the embedding-based semantic component (faster)")
    args = parser.parse_args()

    from app.core.settings import get_settings, get_var_dir
    from app.db.bootstrap import resolve_sqlite_path
    from app.evaluation.replay import aggregate, run_replay, sample_pairs, to_report

    db_path = resolve_sqlite_path(get_settings().database_url)
    database_url = f"sqlite:///{db_path}"
    out_path = args.out or (get_var_dir() / "replay_backtest.json")

    embed_fn = None
    if not args.no_semantic:
        try:
            from app.core.embeddings import get_embedding

            embed_fn = get_embedding
        except Exception:
            print("[warn] embeddings unavailable — running without the semantic component")

    cases = sample_pairs(database_url, n=args.n)
    print(f"Replaying {len(cases)} cases (answers held out of retrieval)…")

    def _progress(i: int, total: int) -> None:
        if i % 10 == 0 or i == total:
            print(f"  {i}/{total}")

    results = run_replay(
        cases,
        database_url=database_url,
        configs_dir=get_settings().configs_dir,
        embed_fn=embed_fn,
        progress=_progress,
    )
    summary = aggregate(results)
    report = to_report(results, summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {out_path}")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
