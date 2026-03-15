from pathlib import Path

from app.ingestion.models import IngestionResult

EXPECTED_EXPORT_FORMAT = """
Expected WhatsApp export input:

- UTF-8 text export from WhatsApp chat export
- one message per line in the typical exported transcript format
- optional media omitted for the first implementation

Example:
12/31/25, 9:41 PM - Alice: Message text
12/31/25, 9:45 PM - User: Reply text
""".strip()


def ingest_whatsapp_export(export_path: Path) -> IngestionResult:
    return IngestionResult(
        source_type="whatsapp_export",
        status="stub",
        detail=(
            "WhatsApp export ingestion is intentionally stubbed. "
            f"Received path: {export_path}. Expected format:\n{EXPECTED_EXPORT_FORMAT}"
        ),
    )
