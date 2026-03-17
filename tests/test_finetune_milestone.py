"""Tests for fine-tune milestone helper."""

import sqlite3
from pathlib import Path

from scripts.finetune_milestone import _count_quality_pairs


def test_count_quality_pairs(tmp_path: Path) -> None:
    db_path = tmp_path / "youos.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY,
                edited_reply TEXT,
                rating INTEGER,
                edit_distance_pct REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO feedback_pairs(edited_reply, rating, edit_distance_pct) VALUES(?, ?, ?)",
            [
                ("Strong reply with substance", 5, 0.10),
                ("Another strong reply here", 4, 0.20),
                ("too short", 5, 0.10),
                ("Bad rating should fail despite length", 3, 0.10),
                ("Too much editing should fail threshold", 5, 0.50),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    assert _count_quality_pairs(db_path) == 2
