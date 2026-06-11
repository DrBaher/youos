"""Demote signature-only "replies" in the reply-pair corpus (b235).

The 2026-06-11 replay backtest found ~18% of sampled pairs had a reply that
was just the user's signature block or a bare "FYI." forward — poisoning
fine-tuning, retrieval exemplars, and eval ground truth. This script finds
them corpus-wide and sets ``quality_score = 0`` (retrieval, auto-feedback and
the backtest all filter on ``quality_score > 0``; nothing is deleted, so the
decision is reversible). Derived organic ``feedback_pairs`` copies of demoted
pairs are removed — they're machine-generated duplicates, not user feedback.

Dry-run by default; pass --apply to write.

    YOUOS_DATA_DIR=... python scripts/clean_reply_pairs.py [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))


def main() -> None:
    parser = argparse.ArgumentParser(description="Demote signature-only reply pairs (quality_score=0).")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run report only)")
    parser.add_argument("--samples", type=int, default=5, help="example rows to print")
    args = parser.parse_args()

    from app.core.config import get_user_names
    from app.core.pair_quality import signature_only_reply
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(get_settings().database_url)
    user_names = [n for n in get_user_names() if n]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, reply_text, reply_author, inbound_author "
            "FROM reply_pairs WHERE COALESCE(quality_score, 1.0) > 0"
        ).fetchall()
        junk_ids: list[int] = []
        examples: list[str] = []
        for r in rows:
            if signature_only_reply(r["reply_text"], reply_author=r["reply_author"], user_names=user_names):
                junk_ids.append(int(r["id"]))
                if len(examples) < args.samples:
                    snippet = " ".join((r["reply_text"] or "").split())[:110]
                    examples.append(f"  #{r['id']} from={r['inbound_author'] or '?'}: {snippet!r}")

        print(f"Scanned {len(rows)} active pairs; signature-only: {len(junk_ids)} "
              f"({100 * len(junk_ids) / max(1, len(rows)):.1f}%)")
        for e in examples:
            print(e)

        fb = 0
        if junk_ids:
            placeholders = ",".join("?" for _ in junk_ids)
            fb = conn.execute(
                f"SELECT COUNT(*) FROM feedback_pairs WHERE reply_pair_id IN ({placeholders}) "  # noqa: S608
                "AND feedback_note = 'organic pair — no YouOS draft'",
                junk_ids,
            ).fetchone()[0]
        print(f"Derived organic feedback_pairs to remove: {fb}")

        if not args.apply:
            print("\nDry run — pass --apply to write.")
            return
        if junk_ids:
            placeholders = ",".join("?" for _ in junk_ids)
            conn.execute(
                f"UPDATE reply_pairs SET quality_score = 0 WHERE id IN ({placeholders})",  # noqa: S608
                junk_ids,
            )
            conn.execute(
                f"DELETE FROM feedback_pairs WHERE reply_pair_id IN ({placeholders}) "  # noqa: S608
                "AND feedback_note = 'organic pair — no YouOS draft'",
                junk_ids,
            )
            conn.commit()
        print(f"Applied: {len(junk_ids)} pairs demoted to quality_score=0, {fb} derived feedback rows removed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
