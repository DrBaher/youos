#!/usr/bin/env python3
"""Measure needs-reply triage accuracy against a labelled corpus.

Usage:
    python scripts/eval_triage.py [--corpus PATH] [--threshold 0.6] [--sweep]

The default corpus is ``configs/triage_corpus.jsonl`` — a small set of
labelled, real-shape messages. Point ``--corpus`` at your own JSONL (one
``{"label": true/false, "sender": ..., "subject": ..., "body": ...}`` per line)
to measure against your real mail. ``--sweep`` prints precision/recall/F1 across
a range of thresholds so you can pick ``agent.threshold`` from data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_CORPUS = ROOT_DIR / "configs" / "triage_corpus.jsonl"


def load_corpus(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def main() -> None:
    from app.evaluation.triage_eval import evaluate_triage, threshold_sweep

    parser = argparse.ArgumentParser(description="Triage precision/recall harness")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--sweep", action="store_true", help="Print metrics across thresholds")
    args = parser.parse_args()

    if not args.corpus.exists():
        print(f"corpus not found: {args.corpus}")
        sys.exit(1)

    cases = load_corpus(args.corpus)
    print(f"Triage eval over {len(cases)} labelled cases — {args.corpus.name}\n")

    if args.sweep:
        print(f"{'thresh':>7} {'prec':>6} {'recall':>6} {'f1':>6} {'acc':>6}")
        for r in threshold_sweep(cases):
            print(f"{r['threshold']:>7.2f} {r['precision']:>6.2f} {r['recall']:>6.2f} {r['f1']:>6.2f} {r['accuracy']:>6.2f}")
        print()

    res = evaluate_triage(cases, threshold=args.threshold)
    c = res["confusion"]
    print(f"At threshold {res['threshold']:.2f}:")
    print(f"  precision {res['precision']:.2f}  recall {res['recall']:.2f}  F1 {res['f1']:.2f}  accuracy {res['accuracy']:.2f}")
    print(f"  confusion: TP={c['tp']} FP={c['fp']} TN={c['tn']} FN={c['fn']}")
    if res["errors"]:
        print("\n  Misclassified:")
        for e in res["errors"]:
            print(f"    [{e['kind']}] score={e['score']:.2f}  {e.get('subject')!r}  ← {e.get('sender')}")


if __name__ == "__main__":
    main()
