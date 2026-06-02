#!/usr/bin/env python3
"""One-shot backfill: derive ``draft_events.sender_type`` for historical rows.

Background (b190 diagnosis): the nightly's ~78% "unknown" ``sender_type`` is a
**data-history artifact**, not live misclassification. ``draft_events`` only
started logging ``sender_type`` around 2026-05-28; rows written before that
have ``sender_type IS NULL``, and the stats summarizer folds NULL into the
"unknown" bucket via ``COALESCE(sender_type, 'unknown')``. The live
``classify_sender`` returns ``unknown`` 0% of the time on rows that carry a
``sender`` string.

This script recomputes ``sender_type`` for NULL-typed rows **that still carry a
``sender`` string**, using the (profile-enriched) ``classify_sender``. It is
idempotent and only touches ``sender_type IS NULL`` rows.

IMPORTANT — honest scope:
  * It fixes go-forward and any historical row whose ``sender`` was logged but
    whose ``sender_type`` was NULL.
  * It CANNOT recover rows that were written with NO ``sender`` at all (the
    pre-2026-05-28 drafting path didn't pass the author). Those rows have
    nothing to classify from and stay NULL — legitimately "unknown". On the
    reference baheros instance, *all* NULL rows are of this unrecoverable
    shape, so this backfill changes 0 of them there. It is provided for
    correctness on instances that did log ``sender`` but missed ``sender_type``.

Usage:
    python3 scripts/backfill_draft_events_sender_type.py            # active instance
    python3 scripts/backfill_draft_events_sender_type.py --dry-run  # report only
    python3 scripts/backfill_draft_events_sender_type.py --db /path # explicit DB
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Allow running as script from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.sender import classify_sender  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.bootstrap import resolve_sqlite_path  # noqa: E402


def backfill_draft_event_sender_types(
    db_path: Path, *, database_url: str | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Return a Counter-style dict of {sender_type: backfilled, "skipped_*": N}.

    ``database_url`` (when given) enables profile-enriched classification.
    """
    if not db_path.exists():
        return {"error_db_missing": 1}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_events'"
        ).fetchone()
        if not exists:
            return {"error_table_missing": 1}

        rows = conn.execute(
            "SELECT id, sender FROM draft_events WHERE sender_type IS NULL"
        ).fetchall()

        counts: Counter[str] = Counter()
        updates: list[tuple[str, int]] = []
        for row in rows:
            sender = (row["sender"] or "").strip()
            if not sender:
                # No author was ever logged — nothing to classify from. Leave
                # NULL (the honest "unknown"); do not fabricate a type.
                counts["skipped_no_sender"] += 1
                continue
            st = classify_sender(sender, database_url)
            if st == "unknown":
                # Sender present but unparseable — also leave NULL.
                counts["skipped_unparseable"] += 1
                continue
            updates.append((st, row["id"]))
            counts[st] += 1

        if not dry_run and updates:
            conn.executemany(
                "UPDATE draft_events SET sender_type = ? WHERE id = ?",
                updates,
            )
            conn.commit()

        return dict(counts)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill draft_events.sender_type")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite DB (defaults to active instance)")
    parser.add_argument("--dry-run", action="store_true", help="Count what would be backfilled, but don't write")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser().resolve()
        database_url = f"sqlite:///{db_path}"
    else:
        database_url = get_settings().database_url
        db_path = resolve_sqlite_path(database_url)

    print(f"Backfilling sender_type for draft_events in {db_path}")
    if args.dry_run:
        print("  (dry-run: no writes)")

    counts = backfill_draft_event_sender_types(db_path, database_url=database_url, dry_run=args.dry_run)
    if "error_db_missing" in counts:
        print(f"  ERROR: DB not found at {db_path}")
        sys.exit(1)
    if "error_table_missing" in counts:
        print("  ERROR: draft_events table missing — nothing to backfill")
        sys.exit(1)

    skip_keys = {"skipped_no_sender", "skipped_unparseable"}
    total = sum(v for k, v in counts.items() if k not in skip_keys)
    print(f"  Classified: {total}")
    for st in ("internal", "external_client", "personal", "automated"):
        if st in counts:
            print(f"    {st}: {counts[st]}")
    if counts.get("skipped_no_sender"):
        print(f"  Skipped (no sender logged — unrecoverable, stays unknown): {counts['skipped_no_sender']}")
    if counts.get("skipped_unparseable"):
        print(f"  Skipped (sender present but unparseable): {counts['skipped_unparseable']}")
    print("Done." if not args.dry_run else "Dry run complete — re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
