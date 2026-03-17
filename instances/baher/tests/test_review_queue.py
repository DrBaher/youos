import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app

ROOT_DIR = Path(__file__).resolve().parents[1]


def _seed_db(db_path: Path, *, num_reply_pairs: int = 15) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Seed a document for the reply pairs
        conn.execute(
            """
            INSERT INTO documents (source_type, source_id, title, author, content, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("gmail_thread", "rq-doc-1", "Re: Integration", "Test", "content",
             '{"account_email": "baher@medicus.ai"}'),
        )
        doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        senders = [
            ("John Smith <john@crelio.com>", "external"),
            ("Jane Doe <jane@medicus.ai>", "internal"),
            ("Bob <bob@gmail.com>", "personal"),
        ]
        for i in range(num_reply_pairs):
            sender_name, _ = senders[i % len(senders)]
            inbound = f"This is a test inbound email number {i} with enough text to pass the 50 char filter easily."
            conn.execute(
                """
                INSERT INTO reply_pairs
                    (source_type, source_id, document_id, inbound_text, reply_text,
                     inbound_author, paired_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gmail_reply",
                    f"rq-rp-{i}",
                    doc_id,
                    inbound,
                    f"Reply to email number {i} with enough text for filtering",
                    sender_name,
                    "2025-06-15T10:00:00",
                    "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_client(monkeypatch, db_path: Path) -> TestClient:
    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    return TestClient(create_app())


def _mock_generate(inbound_message, **kwargs):
    """Mock generate_draft that returns a simple draft."""
    from app.generation.service import DraftResponse
    return DraftResponse(
        draft=f"Draft reply to: {inbound_message[:30]}",
        detected_mode="business",
        precedent_used=[],
        retrieval_method="mock",
        confidence="high",
        confidence_reason="mocked",
        model_used="mock",
    )


# ── GET /review-queue/next ─────────────────────────────────────────


def test_review_queue_next_returns_items(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path, num_reply_pairs=15)
    client = _make_client(monkeypatch, db_path)

    with patch("app.api.review_queue_routes.generate_draft", side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
        response = client.get("/review-queue/next?batch_size=10")

    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) == 10
    assert "total_unreviewed" in data
    assert "reviewed_today" in data

    # Check item fields
    item = data["items"][0]
    assert "reply_pair_id" in item
    assert "inbound_text" in item
    assert "inbound_author" in item
    assert "subject" in item
    assert "generated_draft" in item
    assert "sender_profile" in item
    assert "account_email" in item
    assert "paired_at" in item


def test_review_queue_excludes_already_reviewed(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path, num_reply_pairs=15)

    # Mark one reply pair as already reviewed
    conn = sqlite3.connect(db_path)
    rp_id = conn.execute("SELECT id FROM reply_pairs LIMIT 1").fetchone()[0]
    conn.execute(
        """
        INSERT INTO feedback_pairs
            (inbound_text, generated_draft, edited_reply, reply_pair_id)
        VALUES ('x', 'y', 'z', ?)
        """,
        (rp_id,),
    )
    conn.commit()
    conn.close()

    client = _make_client(monkeypatch, db_path)

    with patch("app.api.review_queue_routes.generate_draft", side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
        response = client.get("/review-queue/next?batch_size=15")

    data = response.json()
    returned_ids = {item["reply_pair_id"] for item in data["items"]}
    assert rp_id not in returned_ids


def test_review_queue_skips_forwarded_and_automated(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)

    # Seed document
    conn.execute(
        """
        INSERT INTO documents (source_type, source_id, title, author, content, metadata_json)
        VALUES ('gmail_thread', 'fwd-doc', 'Fwd Test', 'Test', 'content', '{}')
        """
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Forwarded email (should be skipped)
    conn.execute(
        """
        INSERT INTO reply_pairs
            (source_type, source_id, document_id, inbound_text, reply_text,
             inbound_author, metadata_json)
        VALUES ('gmail_reply', 'fwd-1', ?, ?, 'This is a reply to forwarded', 'someone@test.com', '{}')
        """,
        (doc_id, "---------- Forwarded message ---------- original content here that is long enough"),
    )

    # Automated sender (should be skipped)
    conn.execute(
        """
        INSERT INTO reply_pairs
            (source_type, source_id, document_id, inbound_text, reply_text,
             inbound_author, metadata_json)
        VALUES ('gmail_reply', 'auto-1', ?, ?, 'This is a reply to automated', 'no-reply@service.com', '{}')
        """,
        (doc_id, "This is an automated notification that is long enough to pass the 50 char filter."),
    )

    # Short email (should be skipped)
    conn.execute(
        """
        INSERT INTO reply_pairs
            (source_type, source_id, document_id, inbound_text, reply_text,
             inbound_author, metadata_json)
        VALUES ('gmail_reply', 'short-1', ?, 'Too short', 'This is a reply to short msg', 'real@test.com', '{}')
        """,
        (doc_id,),
    )

    # Valid email (should be included)
    conn.execute(
        """
        INSERT INTO reply_pairs
            (source_type, source_id, document_id, inbound_text, reply_text,
             inbound_author, metadata_json)
        VALUES ('gmail_reply', 'valid-1', ?, ?, 'This is a valid reply text long enough', 'real@company.com', '{}')
        """,
        (doc_id, "This is a valid inbound email that should pass all filters and be included in results."),
    )

    conn.commit()
    conn.close()

    client = _make_client(monkeypatch, db_path)

    with patch("app.api.review_queue_routes.generate_draft", side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
        response = client.get("/review-queue/next?batch_size=10")

    data = response.json()
    assert len(data["items"]) == 1
    assert "real@company.com" in data["items"][0]["inbound_author"]


# ── POST /review-queue/submit ──────────────────────────────────────


def test_review_queue_submit_saves(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)

    response = client.post(
        "/review-queue/submit",
        json={
            "reply_pair_id": 1,
            "inbound_text": "Test inbound",
            "generated_draft": "Generated draft text",
            "edited_reply": "Edited reply text",
            "feedback_note": "good",
            "rating": 4,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "saved"
    assert data["total_pairs"] >= 1

    # Verify in DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM feedback_pairs WHERE reply_pair_id = 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["edited_reply"] == "Edited reply text"
    assert row["rating"] == 4


def test_review_queue_duplicate_submit(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)

    body = {
        "reply_pair_id": 1,
        "inbound_text": "Test inbound",
        "generated_draft": "Generated draft",
        "edited_reply": "Edited reply",
        "rating": 4,
    }

    # First submit
    resp1 = client.post("/review-queue/submit", json=body)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "saved"

    # Duplicate submit
    resp2 = client.post("/review-queue/submit", json=body)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "already_submitted"


# ── Sender profile rebuild tests ─────────────────────────────────


def test_sender_profile_rebuild_triggered_every_10_submissions(
    monkeypatch, tmp_path: Path,
) -> None:
    """build_sender_profiles.py should run in background after every 10th submission."""
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path, num_reply_pairs=15)
    client = _make_client(monkeypatch, db_path)

    import app.api.review_queue_routes as rqr
    # Reset the rebuild tracker
    rqr._last_sender_profile_rebuild = 0.0

    with patch("app.api.review_queue_routes.subprocess.Popen") as mock_popen:
        # Submit 10 reviews with unique reply_pair_ids
        for i in range(1, 11):
            resp = client.post(
                "/review-queue/submit",
                json={
                    "reply_pair_id": i,
                    "inbound_text": f"Inbound text {i}",
                    "generated_draft": f"Draft {i}",
                    "edited_reply": f"Edited {i}",
                    "rating": 4,
                },
            )
            assert resp.status_code == 200

        # Should have been called exactly once (at submission 10)
        assert mock_popen.call_count == 1
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "build_sender_profiles.py" in cmd[-1]


def test_sender_profile_rebuild_on_next_after_1_hour(
    monkeypatch, tmp_path: Path,
) -> None:
    """GET /next should trigger rebuild if last rebuild was > 1 hour ago."""
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path, num_reply_pairs=5)
    client = _make_client(monkeypatch, db_path)

    # Set last rebuild to > 1 hour ago
    import time

    import app.api.review_queue_routes as rqr
    rqr._last_sender_profile_rebuild = time.time() - 7200

    with patch("app.api.review_queue_routes.subprocess.Popen") as mock_popen:
        with patch("app.api.review_queue_routes.generate_draft", side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
            resp = client.get("/review-queue/next?batch_size=3")

        assert resp.status_code == 200
        assert mock_popen.call_count == 1
