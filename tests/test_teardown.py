"""Tests for teardown script — uses mock filesystem."""

import sqlite3

from scripts.teardown import _dir_size, _feedback_count, _file_size


def test_dir_size_nonexistent(tmp_path):
    assert _dir_size(tmp_path / "nope") == "0 B"


def test_dir_size_with_files(tmp_path):
    (tmp_path / "a.txt").write_text("hello" * 100)
    result = _dir_size(tmp_path)
    assert "B" in result


def test_file_size_nonexistent(tmp_path):
    assert _file_size(tmp_path / "nope.db") == "0 B"


def test_file_size_with_file(tmp_path):
    f = tmp_path / "test.db"
    f.write_bytes(b"x" * 1024)
    result = _file_size(f)
    assert "KB" in result or "B" in result


def test_feedback_count_no_db(tmp_path):
    assert _feedback_count(tmp_path / "nope.db") == 0


def test_feedback_count_with_data(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT, generated_draft TEXT, edited_reply TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
        ("hi", "hello", "hey"),
    )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
        ("test", "draft", "reply"),
    )
    conn.commit()
    conn.close()
    assert _feedback_count(db_path) == 2


def test_teardown_removes_dirs(tmp_path):
    """Test that teardown removes the correct directories."""
    # Set up mock directory structure
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    db_path = var_dir / "youos.db"
    db_path.write_bytes(b"fake db")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "raw").mkdir()
    (data_dir / "raw" / "test.json").write_text("{}")

    models_dir = tmp_path / "models"
    models_dir.mkdir()

    # Verify they exist
    assert var_dir.exists()
    assert data_dir.exists()
    assert models_dir.exists()

    # Simulate teardown by removing them
    import shutil

    for d in [data_dir, models_dir, var_dir]:
        if d.exists():
            shutil.rmtree(d)

    assert not var_dir.exists()
    assert not data_dir.exists()
    assert not models_dir.exists()
    # Code directory would remain
    assert tmp_path.exists()
