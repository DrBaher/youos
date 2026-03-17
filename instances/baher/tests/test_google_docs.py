import json
import sqlite3
from pathlib import Path

from app.ingestion.google_docs import DEFAULT_DRIVE_QUERY, GogDocsLiveOptions, ingest_google_docs


def test_ingest_google_docs_live_via_gog(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    cache_dir = tmp_path / "google-docs-cache"
    commands: list[list[str]] = []

    class Completed:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    long_paragraph = " ".join(f"paragraph{i}" for i in range(600))
    doc_text = (
        "Opening summary for Baher mode.\n\n"
        "Second section with more detail.\n\n"
        f"{long_paragraph}"
    )

    def fake_run(command: list[str], *, check: bool, capture_output: bool, text: bool) -> Completed:
        del check, capture_output, text
        commands.append(command)
        if command[:3] == ["gog", "drive", "search"]:
            return Completed(json.dumps([{"id": "doc-live-1", "name": "Strategy Memo"}]))

        if command[:3] == ["gog", "docs", "info"]:
            return Completed(json.dumps({"documentId": "doc-live-1", "title": "Strategy Memo"}))

        if command[:3] == ["gog", "drive", "get"]:
            return Completed(
                json.dumps(
                    {
                        "id": "doc-live-1",
                        "name": "Strategy Memo",
                        "webViewLink": "https://docs.google.com/document/d/doc-live-1/edit",
                        "createdTime": "2026-03-01T09:00:00Z",
                        "modifiedTime": "2026-03-04T15:45:00Z",
                        "owners": [
                            {
                                "displayName": "Baher",
                                "emailAddress": "drbaher@gmail.com",
                            }
                        ],
                        "lastModifyingUser": {
                            "displayName": "Baher",
                            "emailAddress": "drbaher@gmail.com",
                        },
                    }
                )
            )

        if command[:3] == ["gog", "docs", "cat"]:
            return Completed(doc_text)

        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("app.ingestion.google_docs.subprocess.run", fake_run)

    result = ingest_google_docs(
        db_path=db_path,
        live=GogDocsLiveOptions(
            accounts=("drbaher@gmail.com",),
            query=DEFAULT_DRIVE_QUERY,
            max_docs=10,
            cache_dir=cache_dir,
        ),
    )

    assert result.status == "completed"
    assert result.run_id is not None
    assert any(command[:3] == ["gog", "drive", "search"] for command in commands)
    assert any(command[:3] == ["gog", "docs", "info"] for command in commands)
    assert any(command[:3] == ["gog", "drive", "get"] for command in commands)
    assert any(command[:3] == ["gog", "docs", "cat"] for command in commands)
    assert (cache_dir / "drbaher_gmail.com" / "doc-live-1.json").exists()

    connection = sqlite3.connect(db_path)
    try:
        document_row = connection.execute(
            """
            SELECT title, author, external_uri, created_at, updated_at, metadata_json
            FROM documents
            WHERE source_type = 'google_doc' AND source_id = 'doc-live-1'
            """
        ).fetchone()
        chunk_rows = connection.execute(
            """
            SELECT chunk_index, content, metadata_json
            FROM chunks
            ORDER BY chunk_index
            """
        ).fetchall()
        run_row = connection.execute(
            """
            SELECT source, status, discovered_count, fetched_count, stored_document_count, stored_chunk_count
            FROM ingest_runs
            WHERE run_id = ?
            """,
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert document_row is not None
    metadata = json.loads(document_row[5])
    assert document_row[:5] == (
        "Strategy Memo",
        "Baher <drbaher@gmail.com>",
        "https://docs.google.com/document/d/doc-live-1/edit",
        "2026-03-01T09:00:00Z",
        "2026-03-04T15:45:00Z",
    )
    assert metadata["account_email"] == "drbaher@gmail.com"
    assert metadata["source"] == "gog_docs"
    assert metadata["doc_id"] == "doc-live-1"
    assert metadata["owner"] == "Baher <drbaher@gmail.com>"
    assert metadata["missing_fields"] == []
    assert len(chunk_rows) >= 2
    assert json.loads(chunk_rows[0][2])["chunk_role"] == "document_text"
    assert run_row == ("google_docs", "completed", 1, 1, 1, len(chunk_rows))


def test_ingest_google_docs_from_cached_snapshot(tmp_path: Path) -> None:
    export_path = tmp_path / "docs-cache.json"
    db_path = tmp_path / "baheros.db"
    export_path.write_text(
        json.dumps(
            {
                "snapshot_type": "gog_google_doc",
                "doc_id": "doc-cache-1",
                "account": "baher@medicus.ai",
                "source": "gog_docs",
                "query": DEFAULT_DRIVE_QUERY,
                "fetched_at": "2026-03-05T10:00:00Z",
                "docs_info": {"title": "Clinical Notes"},
                "drive_file": {
                    "id": "doc-cache-1",
                    "name": "Clinical Notes",
                    "owners": [{"displayName": "Baher"}],
                },
                "content_text": "First paragraph.\n\nSecond paragraph.",
            }
        ),
        encoding="utf-8",
    )

    result = ingest_google_docs(export_path, db_path=db_path)

    assert result.status == "completed"
    assert result.run_id is not None

    connection = sqlite3.connect(db_path)
    try:
        document_row = connection.execute(
            """
            SELECT title, author, updated_at, metadata_json
            FROM documents
            WHERE source_type = 'google_doc' AND source_id = 'doc-cache-1'
            """
        ).fetchone()
        chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        connection.close()

    assert document_row is not None
    metadata = json.loads(document_row[3])
    assert document_row[:3] == ("Clinical Notes", "Baher", None)
    assert chunk_count == 1
    assert metadata["account_email"] == "baher@medicus.ai"
    assert metadata["missing_fields"] == ["updated_at", "external_uri"]


def test_ingest_google_docs_live_reports_drive_api_disabled(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"

    class Completed:
        def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(command: list[str], *, check: bool, capture_output: bool, text: bool) -> Completed:
        del check, capture_output, text
        if command[:3] == ["gog", "drive", "search"]:
            return Completed(
                stderr=(
                    "SERVICE_DISABLED: Google Drive API has not been used in project 123456 before "
                    "or it is disabled."
                ),
                returncode=1,
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("app.ingestion.google_docs.subprocess.run", fake_run)

    result = ingest_google_docs(
        db_path=db_path,
        live=GogDocsLiveOptions(
            accounts=("drbaher@gmail.com",),
            query=DEFAULT_DRIVE_QUERY,
            max_docs=10,
        ),
    )

    assert result.status == "failed"
    assert result.run_id is not None
    assert "Google Drive API is not enabled for the gog project/environment" in result.detail
    assert "use cached BaherOS Docs snapshots" in result.detail

    connection = sqlite3.connect(db_path)
    try:
        run_row = connection.execute(
            "SELECT status, error_summary FROM ingest_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert run_row is not None
    assert run_row[0] == "failed"
    assert "Failed to load Google Docs import" in run_row[1]


def test_ingest_google_docs_fails_when_only_blank_docs_are_fetched(tmp_path: Path) -> None:
    export_path = tmp_path / "docs-cache.json"
    db_path = tmp_path / "baheros.db"
    export_path.write_text(
        json.dumps(
            {
                "snapshot_type": "gog_google_doc",
                "doc_id": "doc-empty-1",
                "account": "baher@medicus.ai",
                "source": "gog_docs",
                "content_text": "   ",
            }
        ),
        encoding="utf-8",
    )

    result = ingest_google_docs(export_path, db_path=db_path)

    assert result.status == "failed"
    assert "no useful Google Docs corpus rows landed" in result.detail

    connection = sqlite3.connect(db_path)
    try:
        run_row = connection.execute(
            "SELECT status, stored_document_count, stored_chunk_count FROM ingest_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()

    assert run_row == ("failed", 0, 0)
