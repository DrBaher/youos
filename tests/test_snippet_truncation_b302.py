"""b302: raw Gmail API messages must use MIME parts, not the snippet.

A raw Gmail API message (the shape ``gog gmail thread get --full`` returns)
carries a top-level ``snippet`` — a ~200-char, HTML-escaped preview — alongside
its full MIME ``payload``. ``_message_body_text`` listed ``snippet`` among the
direct body fields, which run before payload parsing, so every live-ingested
body was the truncated escaped preview: the ENTIRE documents/reply_pairs corpus
(12,137 pairs, avg 199 chars) and, through it, the finetune training data,
retrieval exemplars, and eval ground truth. Discovered 2026-07-18 while chasing
``&#39;``/``&amp;`` mojibake in reply_pairs.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

from app.ingestion.gmail_threads import _message_body_text, ingest_gmail_threads

FULL_BODY = (
    "Hi Christian, thank you for sending over the mutual NDA. It's largely fine "
    "and I'm keen to move forward quickly. Two redlines from our side: the term "
    "should be three years rather than five, and the governing law should be "
    "Austrian rather than German. I've attached a marked-up version — if those "
    "work for you, we can sign this week. Best, Baher"
)
SNIPPET = (
    "Hi Christian, thank you for sending over the mutual NDA. It&#39;s largely "
    "fine and I&#39;m keen to move forward"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _raw_gmail_message(
    mid: str = "m1",
    *,
    body: str | None = FULL_BODY,
    snippet: str = SNIPPET,
    sender: str = "Baher <me@example.com>",
    to: str = "Christian Beyer <christian@lonvita.io>",
) -> dict:
    """A message in the raw Gmail API shape gog returns with ``--full``."""
    payload: dict = {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "From", "value": sender},
            {"name": "To", "value": to},
            {"name": "Subject", "value": "Mutual NDA — redlines"},
            {"name": "Date", "value": "Fri, 10 Jul 2026 10:00:00 +0200"},
        ],
        "parts": [],
    }
    if body is not None:
        payload["parts"].append(
            {"mimeType": "text/plain", "body": {"data": _b64(body)}}
        )
    return {
        "id": mid,
        "threadId": "t1",
        "labelIds": ["SENT"],
        "snippet": snippet,
        "internalDate": "1783600000000",
        "payload": payload,
    }


def test_full_mime_part_beats_snippet():
    body = _message_body_text(_raw_gmail_message())
    assert body == FULL_BODY
    assert "&#39;" not in body


def test_snippet_only_message_falls_back_unescaped():
    """A message with no decodable parts still yields text — the snippet —
    but with HTML entities unescaped."""
    body = _message_body_text(_raw_gmail_message(body=None))
    assert body.startswith("Hi Christian, thank you")
    assert "It's largely" in body
    assert "&#39;" not in body


def test_export_body_text_field_still_wins():
    """Export formats that pre-extracted a full body keep working."""
    msg = _raw_gmail_message()
    msg["body_text"] = "Pre-extracted full body from an export file."
    assert _message_body_text(msg) == "Pre-extracted full body from an export file."


def test_end_to_end_ingest_stores_full_bodies(tmp_path: Path):
    """Raw-Gmail-shaped thread → reply_pairs carry the FULL reply text."""
    inbound_body = (
        "Hi Baher, please find attached our standard mutual NDA for the "
        "exploration we discussed. Let me know if you have any comments — "
        "otherwise happy to countersign whenever suits."
    )
    thread = {
        "thread_id": "t1",
        "account": "me@example.com",
        "messages": [
            _raw_gmail_message(
                "m-in",
                body=inbound_body,
                snippet="Hi Baher, please find attached our standard mutual NDA",
                sender="Christian Beyer <christian@lonvita.io>",
                to="Baher <me@example.com>",
            )
            | {"labelIds": ["INBOX"]},
            _raw_gmail_message("m-out"),
        ],
    }
    export = tmp_path / "threads.json"
    export.write_text(json.dumps({"threads": [thread]}), encoding="utf-8")
    db = tmp_path / "youos.db"
    result = ingest_gmail_threads(export, db_path=db, user_emails=("me@example.com",))
    assert result.status == "completed"

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        pair = conn.execute("SELECT * FROM reply_pairs").fetchone()
        doc = conn.execute("SELECT * FROM documents").fetchone()
    finally:
        conn.close()
    assert pair["reply_text"] == FULL_BODY, "reply must be the full body, not the snippet"
    assert pair["inbound_text"] == inbound_body
    assert doc["content"] == inbound_body
    assert "&#39;" not in pair["reply_text"]
