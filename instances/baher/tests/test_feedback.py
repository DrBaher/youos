import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app

ROOT_DIR = Path(__file__).resolve().parents[1]


def _seed_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Seed minimal data for retrieval
        conn.execute(
            """
            INSERT INTO documents (source_type, source_id, title, author, content, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("gmail_thread", "fb-m1", "Test", "Test", "test content", '{"account_email": "test@test.com"}'),
        )
        conn.commit()
    finally:
        conn.close()


# ── F1: Schema ──────────────────────────────────────────────────────


def test_feedback_pairs_schema_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Table exists and accepts inserts
        conn.execute(
            """
            INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply)
            VALUES ('hello', 'draft', 'edited')
            """
        )
        conn.commit()
        row = conn.execute("SELECT * FROM feedback_pairs").fetchone()
        assert row is not None
        # Check columns by name
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM feedback_pairs").fetchone()
        assert row["inbound_text"] == "hello"
        assert row["generated_draft"] == "draft"
        assert row["edited_reply"] == "edited"
        assert row["used_in_finetune"] == 0
        assert row["created_at"] is not None
    finally:
        conn.close()


# ── F2: Feedback submit route ───────────────────────────────────────


def test_feedback_submit_saves_to_db(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.post(
        "/feedback/submit",
        json={
            "inbound_text": "Please send pricing",
            "generated_draft": "Here is our pricing.",
            "edited_reply": "Hi, attached is our pricing sheet.",
            "feedback_note": "added greeting",
            "rating": 4,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "saved"
    assert data["total_pairs"] == 1

    # Verify in DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM feedback_pairs WHERE id = 1").fetchone()
        assert row["inbound_text"] == "Please send pricing"
        assert row["edited_reply"] == "Hi, attached is our pricing sheet."
        assert row["rating"] == 4
        assert row["feedback_note"] == "added greeting"
    finally:
        conn.close()


def test_edit_distance_pct_computed_on_submit(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.post(
        "/feedback/submit",
        json={
            "inbound_text": "Please send pricing",
            "generated_draft": "Here is our pricing.",
            "edited_reply": "Here is our pricing.",  # identical → 0.0
            "rating": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "edit_distance_pct" in data
    assert data["edit_distance_pct"] == 0.0

    # Verify stored in DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT edit_distance_pct FROM feedback_pairs WHERE id = 1").fetchone()
        assert row["edit_distance_pct"] == 0.0
    finally:
        conn.close()


def test_edit_distance_pct_nonzero_for_different_text(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.post(
        "/feedback/submit",
        json={
            "inbound_text": "Hello",
            "generated_draft": "Here is our pricing breakdown for the enterprise plan.",
            "edited_reply": "Completely different text that has nothing in common whatsoever.",
            "rating": 2,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["edit_distance_pct"] > 0.0
    assert data["edit_distance_pct"] <= 1.0


def test_feedback_submit_validates_empty(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.post(
        "/feedback/submit",
        json={
            "inbound_text": "",
            "generated_draft": "draft",
            "edited_reply": "reply",
        },
    )
    assert response.status_code == 422


def test_feedback_page_returns_html(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.get("/feedback")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "BaherOS" in response.text


# ── F3: JSONL exporter format ───────────────────────────────────────


def test_jsonl_exporter_format(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        for i in range(5):
            conn.execute(
                """
                INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating)
                VALUES (?, ?, ?, ?)
                """,
                (f"inbound {i}", f"draft {i}", f"reply {i}", 5),
            )
        conn.commit()
    finally:
        conn.close()

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    train_path = output_dir / "train.jsonl"

    result = subprocess.run(
        [
            sys.executable, str(ROOT_DIR / "scripts" / "export_feedback_jsonl.py"),
            "--all",
            "--db", str(db_path),
            "--output", str(train_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Exported 5 pairs" in result.stdout

    # Verify JSONL format
    lines = train_path.read_text(encoding="utf-8").strip().splitlines()
    valid_path = output_dir / "valid.jsonl"
    if valid_path.exists():
        lines += valid_path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 5
    for line in lines:
        record = json.loads(line)
        assert "messages" in record
        msgs = record["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


def test_jsonl_exporter_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
    finally:
        conn.close()

    result = subprocess.run(
        [
            sys.executable, str(ROOT_DIR / "scripts" / "export_feedback_jsonl.py"),
            "--all",
            "--db", str(db_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "0 pairs" in result.stdout


# ── F4: Finetune dry-run ────────────────────────────────────────────


def test_finetune_dry_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable, str(ROOT_DIR / "scripts" / "finetune_lora.py"),
            "--dry-run",
            "--data-dir", str(data_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "dry-run" in result.stdout.lower()
    assert "Qwen/Qwen2.5-1.5B-Instruct" in result.stdout


# ── F5: Generation fallback ────────────────────────────────────────


def test_generation_falls_back_to_claude_no_adapter(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    with patch("app.generation.service._adapter_available", return_value=False), \
         patch("app.generation.service._call_claude_cli", return_value="Claude draft."):
        from app.generation.service import DraftRequest, generate_draft

        response = generate_draft(
            DraftRequest(inbound_message="Hello", use_local_model=True),
            database_url=f"sqlite:///{db_path}",
            configs_dir=ROOT_DIR / "configs",
        )

    assert response.model_used == "claude"
    assert response.draft == "Claude draft."


def test_generation_uses_local_model_when_adapter_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    with patch("app.generation.service._adapter_available", return_value=True), \
         patch("app.generation.service._call_local_model", return_value="Local draft."):
        from app.generation.service import DraftRequest, generate_draft

        response = generate_draft(
            DraftRequest(inbound_message="Hello", use_local_model=True),
            database_url=f"sqlite:///{db_path}",
            configs_dir=ROOT_DIR / "configs",
        )

    assert response.model_used == "qwen2.5-1.5b-lora"
    assert response.draft == "Local draft."


def test_generation_skips_local_when_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    with patch("app.generation.service._adapter_available", return_value=True), \
         patch("app.generation.service._call_claude_cli", return_value="Claude draft."):
        from app.generation.service import DraftRequest, generate_draft

        response = generate_draft(
            DraftRequest(inbound_message="Hello", use_local_model=False),
            database_url=f"sqlite:///{db_path}",
            configs_dir=ROOT_DIR / "configs",
        )

    assert response.model_used == "claude"
