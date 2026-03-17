import json
import sqlite3
from pathlib import Path

from app.ingestion.gmail_threads import GogLiveOptions, ingest_gmail_threads


def test_ingest_gmail_threads_stores_documents_and_reply_pairs(tmp_path: Path) -> None:
    export_path = tmp_path / "gmail.json"
    db_path = tmp_path / "baheros.db"
    export_path.write_text(
        json.dumps(
            {
                "thread_id": "thread-1",
                "subject": "Draft review",
                "messages": [
                    {
                        "id": "m1",
                        "timestamp": "2026-03-01T09:00:00Z",
                        "from_email": "alice@example.com",
                        "from_name": "Alice",
                        "body_text": "Can you send the draft?",
                    },
                    {
                        "id": "m2",
                        "timestamp": "2026-03-01T09:15:00Z",
                        "from_email": "baher@example.com",
                        "from_name": "Baher",
                        "label_ids": ["SENT"],
                        "body_text": "Attached. Let me know what you want changed.",
                    },
                    {
                        "id": "m3",
                        "timestamp": "2026-03-01T09:20:00Z",
                        "from_email": "alice@example.com",
                        "from_name": "Alice",
                        "body_text": "Please trim the intro.",
                    },
                    {
                        "id": "m4",
                        "timestamp": "2026-03-01T09:22:00Z",
                        "from_email": "sam@example.com",
                        "from_name": "Sam",
                        "body_text": "Keep the examples.",
                    },
                    {
                        "id": "m5",
                        "timestamp": "2026-03-01T09:40:00Z",
                        "from_email": "baher@example.com",
                        "from_name": "Baher",
                        "label_ids": ["SENT"],
                        "body_text": "Makes sense. I will tighten the intro and keep the examples.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ingest_gmail_threads(export_path, db_path=db_path)

    assert result.status == "completed"
    assert result.run_id is not None

    connection = sqlite3.connect(db_path)
    try:
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        run_row = connection.execute(
            """
            SELECT source, status, discovered_count, fetched_count, stored_document_count,
                   stored_chunk_count, stored_reply_pair_count
            FROM ingest_runs
            WHERE run_id = ?
            """,
            (result.run_id,),
        ).fetchone()
        document_metadata = json.loads(
            connection.execute("SELECT metadata_json FROM documents WHERE source_id = 'm1'").fetchone()[0]
        )
        pair_rows = connection.execute(
            """
            SELECT inbound_text, reply_text, inbound_author, reply_author, metadata_json
            FROM reply_pairs
            ORDER BY source_id
            """
        ).fetchall()
    finally:
        connection.close()

    assert document_count == 3
    assert chunk_count == 3
    assert run_row == ("gmail", "completed", 1, 1, 3, 3, 2)
    assert len(pair_rows) == 2
    assert pair_rows[0][0] == "Can you send the draft?"
    assert "Please trim the intro." in pair_rows[1][0]
    assert "Keep the examples." in pair_rows[1][0]
    assert pair_rows[1][1] == "Makes sense. I will tighten the intro and keep the examples."
    assert pair_rows[1][2] == "Sam <sam@example.com>"
    assert pair_rows[1][3] == "Baher <baher@example.com>"
    assert document_metadata["source"] == "json_import"
    assert document_metadata["sender"]["email"] == "alice@example.com"
    assert json.loads(pair_rows[1][4])["pair_strategy"] == "messages_since_last_self_authored_message"


def test_ingest_gmail_threads_supports_gmail_api_style_payload(tmp_path: Path) -> None:
    export_path = tmp_path / "thread.json"
    db_path = tmp_path / "baheros.db"
    export_path.write_text(
        json.dumps(
            {
                "id": "thread-raw",
                "messages": [
                    {
                        "id": "raw-1",
                        "threadId": "thread-raw",
                        "internalDate": "1761997200000",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Alice <alice@example.com>"},
                                {"name": "Subject", "value": "Status"},
                            ],
                            "mimeType": "text/plain",
                            "body": {"data": "SGkgQmFoZXIsIGNhbiB5b3UgcmV2aWV3IHRoaXM_"},
                        },
                    },
                    {
                        "id": "raw-2",
                        "threadId": "thread-raw",
                        "internalDate": "1761997800000",
                        "labelIds": ["SENT"],
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Baher <baher@example.com>"},
                                {"name": "Subject", "value": "Status"},
                            ],
                            "mimeType": "text/plain",
                            "body": {"data": "U3VyZS4gSSdsbCBzZW5kIG5vdGVzIHRvZGF5Lg=="},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ingest_gmail_threads(export_path, db_path=db_path)

    assert result.status == "completed"
    assert result.run_id is not None

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT inbound_text, reply_text FROM reply_pairs"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("Hi Baher, can you review this?", "Sure. I'll send notes today.")


def test_ingest_gmail_threads_live_via_gog(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    cache_dir = tmp_path / "gmail-cache"
    commands: list[list[str]] = []

    class Completed:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(command: list[str], *, check: bool, capture_output: bool, text: bool) -> Completed:
        del check, capture_output, text
        commands.append(command)
        if command[:3] == ["gog", "gmail", "search"]:
            return Completed(json.dumps([{"thread": {"id": "thread-live-1"}, "subject": "Status"}]))

        if command[:4] == ["gog", "gmail", "thread", "get"]:
            return Completed(
                json.dumps(
                    {
                        "thread": {
                            "messages": [
                                {
                                    "id": "live-1",
                                    "internalDate": "1761997200000",
                                    "payload": {
                                        "headers": [
                                            {"name": "From", "value": "Alice <alice@example.com>"},
                                            {"name": "To", "value": "Baher <drbaher@gmail.com>"},
                                            {"name": "Subject", "value": "Status"},
                                        ],
                                        "mimeType": "text/plain",
                                        "body": {"data": "SGkgQmFoZXIsIGNhbiB5b3UgcmV2aWV3IHRoaXM_"},
                                    },
                                },
                                {
                                    "id": "live-2",
                                    "internalDate": "1761997800000",
                                    "payload": {
                                        "headers": [
                                            {"name": "From", "value": "Baher <drbaher@gmail.com>"},
                                            {"name": "To", "value": "Alice <alice@example.com>"},
                                            {"name": "Subject", "value": "Status"},
                                        ],
                                        "mimeType": "text/plain",
                                        "body": {"data": "U3VyZS4gSSdsbCBzZW5kIG5vdGVzIHRvZGF5Lg=="},
                                    },
                                },
                            ]
                        },
                    }
                )
            )

        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("app.ingestion.gmail_threads.subprocess.run", fake_run)

    result = ingest_gmail_threads(
        db_path=db_path,
        live=GogLiveOptions(
            accounts=("drbaher@gmail.com",),
            query="label:inbox newer_than:30d",
            max_threads=10,
            cache_dir=cache_dir,
        ),
    )

    assert result.status == "completed_with_warnings"
    assert result.run_id is not None
    assert any(command[:3] == ["gog", "gmail", "search"] for command in commands)
    assert any(command[:4] == ["gog", "gmail", "thread", "get"] for command in commands)
    assert (cache_dir / "drbaher_gmail.com" / "thread-live-1.json").exists()

    connection = sqlite3.connect(db_path)
    try:
        doc_row = connection.execute(
            "SELECT metadata_json FROM documents WHERE source_id = 'live-1'"
        ).fetchone()
        pair_row = connection.execute(
            "SELECT inbound_text, reply_text, reply_author, metadata_json FROM reply_pairs"
        ).fetchone()
        run_row = connection.execute(
            "SELECT status, discovered_count, fetched_count, stored_document_count, stored_reply_pair_count "
            "FROM ingest_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert doc_row is not None
    document_metadata = json.loads(doc_row[0])
    assert document_metadata["account_email"] == "drbaher@gmail.com"
    assert document_metadata["source"] == "gog_gmail"
    assert document_metadata["recipients"]["to"][0]["email"] == "drbaher@gmail.com"
    assert document_metadata["ingestion_warnings"] == [
        "gog gmail thread get returned messages without a thread id; BaherOS used the requested thread id."
    ]

    assert pair_row is not None
    pair_metadata = json.loads(pair_row[3])
    assert pair_row[:3] == (
        "Hi Baher, can you review this?",
        "Sure. I'll send notes today.",
        "Baher <drbaher@gmail.com>",
    )
    assert pair_metadata["account_email"] == "drbaher@gmail.com"
    assert pair_metadata["source"] == "gog_gmail"
    assert pair_metadata["inbound_message_ids"] == ["live-1"]
    assert pair_metadata["reply_recipient_context"]["to"][0]["email"] == "alice@example.com"
    assert run_row == ("completed_with_warnings", 1, 1, 1, 1)


def test_ingest_gmail_threads_live_fails_clearly_when_search_result_has_no_thread_id(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "baheros.db"

    class Completed:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(command: list[str], *, check: bool, capture_output: bool, text: bool) -> Completed:
        del check, capture_output, text
        if command[:3] == ["gog", "gmail", "search"]:
            return Completed(json.dumps([{"thread": {"subject": "Missing id"}}]))
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("app.ingestion.gmail_threads.subprocess.run", fake_run)

    result = ingest_gmail_threads(
        db_path=db_path,
        live=GogLiveOptions(
            accounts=("drbaher@gmail.com",),
            query="label:inbox newer_than:30d",
            max_threads=10,
        ),
    )

    assert result.status == "failed"
    assert result.run_id is not None
    assert "returned a result without a thread id" in result.detail

    connection = sqlite3.connect(db_path)
    try:
        run_row = connection.execute(
            "SELECT status, error_summary, discovered_count, fetched_count FROM ingest_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert run_row is not None
    assert run_row[0] == "failed"
    assert "Failed to load Gmail import" in run_row[1]
    assert run_row[2:] == (1, 0)


def test_ingest_gmail_threads_fails_when_no_useful_rows_land(tmp_path: Path) -> None:
    export_path = tmp_path / "gmail-empty.json"
    db_path = tmp_path / "baheros.db"
    export_path.write_text(
        json.dumps(
            {
                "thread_id": "thread-empty",
                "messages": [
                    {
                        "id": "m1",
                        "timestamp": "2026-03-01T09:00:00Z",
                        "from_email": "baher@example.com",
                        "from_name": "Baher",
                        "label_ids": ["SENT"],
                        "body_text": "Internal draft note.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ingest_gmail_threads(export_path, db_path=db_path)

    assert result.status == "failed"
    assert "no useful Gmail corpus rows landed" in result.detail

    connection = sqlite3.connect(db_path)
    try:
        run_row = connection.execute(
            "SELECT status, stored_document_count, stored_reply_pair_count FROM ingest_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert run_row == ("failed", 0, 0)
