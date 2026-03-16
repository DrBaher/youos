"""WhatsApp chat export ingestion for YouOS."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.config import get_user_names
from app.ingestion.models import IngestionResult
from app.ingestion.run_log import IngestRunContext, IngestRunCounts, finish_ingest_run, start_ingest_run

EXPECTED_EXPORT_FORMAT = """
Expected WhatsApp export input:

- UTF-8 text export from WhatsApp chat export
- one message per line in the typical exported transcript format
- optional media omitted for the first implementation

Example:
12/31/25, 9:41 PM - Alice: Message text
12/31/25, 9:45 PM - User: Reply text
""".strip()

# Regex: MM/DD/YY, H:MM AM/PM - Sender: Message
LINE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*[APap][Mm])\s*-\s*(.+?):\s(.+)$")


@dataclass(slots=True)
class ParsedMessage:
    timestamp: str
    sender: str
    text: str


@dataclass(slots=True)
class WhatsAppIngestCounts:
    total_lines: int = 0
    parsed_messages: int = 0
    inbound_documents: int = 0
    reply_pairs: int = 0


def parse_whatsapp_export(text: str) -> list[ParsedMessage]:
    """Parse a WhatsApp text export into structured messages."""
    messages: list[ParsedMessage] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if m:
            messages.append(
                ParsedMessage(
                    timestamp=m.group(1).strip(),
                    sender=m.group(2).strip(),
                    text=m.group(3).strip(),
                )
            )
        elif messages:
            # Continuation line — append to previous message
            messages[-1].text += "\n" + line
    return messages


def _is_user_message(sender: str, user_names: tuple[str, ...]) -> bool:
    """Check if sender matches any configured user name (case-insensitive)."""
    sender_lower = sender.lower()
    for name in user_names:
        if name.lower() == sender_lower:
            return True
    return False


def build_reply_pairs(
    messages: list[ParsedMessage],
    user_names: tuple[str, ...],
) -> list[tuple[ParsedMessage, ParsedMessage]]:
    """Pair adjacent inbound + user-reply messages."""
    pairs: list[tuple[ParsedMessage, ParsedMessage]] = []
    i = 0
    while i < len(messages) - 1:
        inbound = messages[i]
        reply = messages[i + 1]
        if not _is_user_message(inbound.sender, user_names) and _is_user_message(reply.sender, user_names):
            pairs.append((inbound, reply))
            i += 2  # skip past both
        else:
            i += 1
    return pairs


def _parse_timestamp(ts: str) -> str | None:
    """Convert WhatsApp timestamp to ISO format."""
    for fmt in ("%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p"):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def ingest_whatsapp_export(
    export_path: Path,
    *,
    db_path: Path | None = None,
    user_names: tuple[str, ...] = (),
) -> IngestionResult:
    """Parse a WhatsApp export file and ingest into the corpus."""
    if not export_path.exists():
        return IngestionResult(
            source_type="whatsapp_export",
            status="failed",
            detail=f"File not found: {export_path}",
        )

    text = export_path.read_text(encoding="utf-8")
    messages = parse_whatsapp_export(text)

    if not user_names:
        user_names = get_user_names()

    counts = WhatsAppIngestCounts(total_lines=len(text.splitlines()), parsed_messages=len(messages))

    if not messages:
        return IngestionResult(
            source_type="whatsapp_export",
            status="completed",
            detail="No messages parsed from export file.",
        )

    pairs = build_reply_pairs(messages, user_names)

    if db_path is None:
        from app.core.settings import get_settings
        from app.db.bootstrap import resolve_sqlite_path

        settings = get_settings()
        db_path = resolve_sqlite_path(settings.database_url)

    run_id = f"whatsapp-{uuid.uuid4()}"
    conn = sqlite3.connect(db_path)
    try:
        context = IngestRunContext(run_id=run_id, source="whatsapp")
        start_ingest_run(conn, context)
        conn.commit()

        chat_name = export_path.stem
        run_counts = IngestRunCounts(discovered=len(messages), fetched=len(messages))

        # Insert inbound messages as documents
        for msg in messages:
            if _is_user_message(msg.sender, user_names):
                continue
            source_id = f"wa-{chat_name}-{msg.timestamp}-{msg.sender}"
            iso_ts = _parse_timestamp(msg.timestamp)
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO documents
                       (source_type, source_id, title, author, content, metadata_json,
                        ingestion_run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "whatsapp_export",
                        source_id,
                        f"WhatsApp: {chat_name}",
                        msg.sender,
                        msg.text,
                        json.dumps({"chat": chat_name, "timestamp": msg.timestamp}),
                        run_id,
                        iso_ts,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    counts.inbound_documents += 1
                    run_counts.stored_documents += 1
            except sqlite3.IntegrityError:
                pass

        # Insert reply pairs
        for inbound, reply in pairs:
            pair_source_id = f"wa-{chat_name}-{inbound.timestamp}-{reply.timestamp}"
            iso_ts = _parse_timestamp(reply.timestamp)

            # Find the document_id for this inbound message
            doc_source_id = f"wa-{chat_name}-{inbound.timestamp}-{inbound.sender}"
            doc_row = conn.execute("SELECT id FROM documents WHERE source_id = ?", (doc_source_id,)).fetchone()
            doc_id = doc_row[0] if doc_row else None

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO reply_pairs
                       (source_type, source_id, document_id, inbound_text, reply_text,
                        inbound_author, reply_author, paired_at, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "whatsapp_export",
                        pair_source_id,
                        doc_id,
                        inbound.text,
                        reply.text,
                        inbound.sender,
                        "user",
                        iso_ts,
                        json.dumps({"chat": chat_name}),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    counts.reply_pairs += 1
                    run_counts.stored_reply_pairs += 1
            except sqlite3.IntegrityError:
                pass

        finish_ingest_run(
            conn,
            run_id=run_id,
            status="completed",
            counts=run_counts,
        )
        conn.commit()
    finally:
        conn.close()

    return IngestionResult(
        source_type="whatsapp_export",
        status="completed",
        detail=(
            f"Parsed {counts.parsed_messages} messages, "
            f"stored {counts.inbound_documents} inbound docs, "
            f"{counts.reply_pairs} reply pairs from {export_path.name}"
        ),
        run_id=run_id,
    )
