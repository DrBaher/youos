#!/usr/bin/env python3
"""Deduplicate reply_pairs and documents in the YouOS corpus.

Usage:
    python3 scripts/deduplicate_corpus.py --dry-run   # show duplicates
    python3 scripts/deduplicate_corpus.py              # remove duplicates
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.settings import get_settings  # noqa: E402
from app.db.bootstrap import resolve_sqlite_path  # noqa: E402


def _hash_text(text: str | None) -> str:
    return hashlib.md5((text or "").encode()).hexdigest()


def find_duplicate_reply_pairs(conn: sqlite3.Connection) -> list[int]:
    """Find duplicate reply_pairs by (source_type, source_id) or (thread_id, inbound_text hash)."""
    dupe_ids: list[int] = []

    # Duplicates by (source_type, source_id)
    rows = conn.execute(
        """
        SELECT source_type, source_id, GROUP_CONCAT(id) as ids, COUNT(*) as cnt
        FROM reply_pairs
        WHERE source_id IS NOT NULL
        GROUP BY source_type, source_id
        HAVING cnt > 1
        """
    ).fetchall()
    for row in rows:
        ids = [int(x) for x in row[2].split(",")]
        dupe_ids.extend(ids[1:])  # Keep first, remove rest

    # Duplicates by (thread_id, inbound_text hash)
    all_pairs = conn.execute("SELECT id, thread_id, inbound_text FROM reply_pairs WHERE thread_id IS NOT NULL").fetchall()
    seen: dict[tuple[str, str], int] = {}
    for pair_id, thread_id, inbound_text in all_pairs:
        key = (thread_id, _hash_text(inbound_text))
        if key in seen:
            if pair_id not in dupe_ids:
                dupe_ids.append(pair_id)
        else:
            seen[key] = pair_id

    return dupe_ids


def find_duplicate_documents(conn: sqlite3.Connection) -> list[int]:
    """Find duplicate documents by (source_type, source_id)."""
    rows = conn.execute(
        """
        SELECT source_type, source_id, GROUP_CONCAT(id) as ids, COUNT(*) as cnt
        FROM documents
        WHERE source_id IS NOT NULL
        GROUP BY source_type, source_id
        HAVING cnt > 1
        """
    ).fetchall()
    dupe_ids: list[int] = []
    for row in rows:
        ids = [int(x) for x in row[2].split(",")]
        dupe_ids.extend(ids[1:])
    return dupe_ids


def deduplicate(dry_run: bool = False) -> dict[str, int]:
    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return {"reply_pairs": 0, "documents": 0, "total": 0}

    conn = sqlite3.connect(db_path)
    try:
        dupe_pairs = find_duplicate_reply_pairs(conn)
        dupe_docs = find_duplicate_documents(conn)

        # Count unique threads among duplicate pairs
        if dupe_pairs:
            placeholders = ",".join("?" * len(dupe_pairs))
            thread_rows = conn.execute(
                f"SELECT DISTINCT thread_id FROM reply_pairs WHERE id IN ({placeholders})",
                dupe_pairs,
            ).fetchall()
            unique_threads = len(thread_rows)
        else:
            unique_threads = 0

        print(f"Found {len(dupe_pairs)} duplicate reply_pairs ({unique_threads} unique threads)")
        print(f"Found {len(dupe_docs)} duplicate documents")

        if dry_run:
            print("Dry run — no rows removed.")
            return {"reply_pairs": len(dupe_pairs), "documents": len(dupe_docs), "total": 0}

        removed = 0
        if dupe_pairs:
            placeholders = ",".join("?" * len(dupe_pairs))
            conn.execute(f"DELETE FROM reply_pairs WHERE id IN ({placeholders})", dupe_pairs)
            removed += len(dupe_pairs)
        if dupe_docs:
            placeholders = ",".join("?" * len(dupe_docs))
            conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", dupe_docs)
            removed += len(dupe_docs)
        conn.commit()
        print(f"Removed {removed} rows total.")
        return {"reply_pairs": len(dupe_pairs), "documents": len(dupe_docs), "total": removed}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate YouOS corpus")
    parser.add_argument("--dry-run", action="store_true", help="Show duplicates without removing")
    args = parser.parse_args()
    deduplicate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
