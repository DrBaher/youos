#!/usr/bin/env python3
"""Agent triage demo — fetch unread → classify → draft → print.

Prototype for Phase 1 of the autonomous-agent loop. NOT a polished feature
yet; this is the smallest end-to-end exercise of the agent idea against your
real Gmail, so you can judge filter + draft quality before I invest in the
full PR.

b178: this used to KEEP/SKIP with a hand-rolled regex (no-reply /
List-Unsubscribe / a few automation domains) that diverged from production —
on the baher@medicus.ai demo it over-kept a conference ticket, an invoice, and
an event newsletter and drafted replies that just restated the mail. The demo
now calls the SAME production needs-reply classifier the real triage sweep uses
(``app.agent.needs_reply.classify``), so what you see here is what the agent
would actually do. Still draft-only / read-only — no send, no persist.

Run from the repo root:

    YOUOS_DATA_DIR=~/YouOS-Instances/baheros \\
    GOG_KEYRING_PASSWORD=... \\
    .venv/bin/python scripts/triage_demo.py
"""
from __future__ import annotations

import base64
import os
import re
import sys
import textwrap
from typing import Any

ACCOUNT = os.environ.get("YOUOS_TRIAGE_ACCOUNT", "drbaher@gmail.com")
WINDOW = os.environ.get("YOUOS_TRIAGE_WINDOW", "3d")
LIMIT = int(os.environ.get("YOUOS_TRIAGE_LIMIT", "8"))
# Same default as run_triage(threshold=0.6) so the demo and the real sweep agree.
THRESHOLD = float(os.environ.get("YOUOS_TRIAGE_THRESHOLD", "0.6"))


def header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def extract_text(payload: dict[str, Any]) -> str:
    """Pull text/plain (or fallback to text/html stripped) from a Gmail payload."""
    def walk(p: dict[str, Any]) -> str:
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        if mime == "text/html" and data:
            html = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)
        for part in p.get("parts", []) or []:
            r = walk(part)
            if r:
                return r
        return ""
    return walk(payload).strip()


def build_message(payload: dict[str, Any], *, sender: str, subject: str, body: str):
    """Construct the same ``InboxMessage`` the production sweep classifies, from
    a raw Gmail payload. Mirrors ``app.agent.inbox_fetch`` field mapping so the
    classifier sees identical inputs (notably the lowercased ``list-unsubscribe``
    header and the parsed sender email)."""
    from app.agent.inbox_fetch import InboxMessage
    from app.core.sender import extract_email

    # Lowercase header names — classify() reads ``headers.get("list-unsubscribe")``.
    headers = {
        (h.get("name", "") or "").lower(): (h.get("value", "") or "")
        for h in (payload.get("headers", []) or [])
    }
    email = (extract_email(sender) or "").lower() or None
    return InboxMessage(
        message_id="demo",
        thread_id="demo",
        account=ACCOUNT,
        sender=sender,
        sender_email=email,
        subject=subject,
        body=body,
        headers=headers,
        received_at=header(payload, "Date") or None,
    )


def decide(payload: dict[str, Any], *, sender: str, subject: str, body: str):
    """Run the PRODUCTION needs-reply classifier on this message and return its
    ``NeedsReplyVerdict``. This is the single source of truth the real triage
    sweep (``app.agent.triage.run_triage`` → ``classify_many``) uses, so the
    demo no longer diverges from production."""
    from app.agent.needs_reply import classify

    msg = build_message(payload, sender=sender, subject=subject, body=body)
    return classify(msg, history=None, threshold=THRESHOLD)


def main() -> int:
    if "GOG_KEYRING_PASSWORD" not in os.environ:
        print("ERROR: GOG_KEYRING_PASSWORD not set; gog can't unlock the keyring.", file=sys.stderr)
        print("       Re-run with `GOG_KEYRING_PASSWORD=... ...`", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)

    from app.core.settings import get_settings
    from app.generation.service import DraftRequest, generate_draft
    from app.ingestion.gmail_threads import _gog_get_thread, _gog_search_threads

    settings = get_settings()

    print("━━━ Agent triage demo ━━━")
    print(f"account:   {ACCOUNT}")
    print(f"instance:  {settings.data_dir or settings.database_url}")
    print(f"window:    {WINDOW}   limit: {LIMIT}   threshold: {THRESHOLD}")
    print("classifier: app.agent.needs_reply.classify (production)\n")

    query = f"in:inbox is:unread newer_than:{WINDOW}"
    threads = _gog_search_threads(account=ACCOUNT, query=query, max_threads=LIMIT)
    print(f"Fetched {len(threads)} unread thread(s).\n")

    kept = skipped = drafted = failed = 0
    for i, t in enumerate(threads[:LIMIT], 1):
        tid = t.get("id") or t.get("threadId") or t.get("Id")
        try:
            thread = _gog_get_thread(account=ACCOUNT, thread_id=tid)
        except Exception as e:
            print(f"[{i}] (couldn't fetch thread {tid}: {e})\n")
            failed += 1
            continue

        messages = thread.get("messages", []) or [thread]
        msg = messages[-1]                  # the latest message in the thread is what's unread
        payload = msg.get("payload", {}) or {}
        sender = header(payload, "From")
        subject = header(payload, "Subject") or "(no subject)"
        body = extract_text(payload)

        # Production needs-reply decision — identical to what run_triage does.
        verdict = decide(payload, sender=sender, subject=subject, body=body)

        if not verdict.needs_reply:
            tag = "SURFACE" if verdict.surface_for_review else "SKIP"
            print(f"[{i}] {tag:4}  {subject!r}")
            print(f"      from {sender}")
            print(f"      → needs_reply=False  score={verdict.score:.2f}")
            if verdict.reasons:
                print(f"      → {'; '.join(verdict.reasons)}\n")
            else:
                print()
            skipped += 1
            continue

        body_excerpt = textwrap.shorten(body, width=240, placeholder="…")
        kept += 1

        print(f"[{i}] KEEP  {subject!r}")
        print(f"      from {sender}")
        print(f"      → needs_reply=True  score={verdict.score:.2f}  ({'; '.join(verdict.reasons)})")
        print(f"      inbound: {body_excerpt}")

        try:
            req = DraftRequest(
                inbound_message=body,
                sender=sender,
                subject=subject,
                account_email=ACCOUNT,
            )
            resp = generate_draft(
                req,
                database_url=settings.database_url,
                configs_dir=settings.configs_dir,
            )
            drafted += 1
            conf = getattr(resp, "confidence", None)
            conf_str = f"  conf: {conf:.2f}" if isinstance(conf, (int, float)) else ""
            print(f"      model: {getattr(resp, 'model_used', '?')}{conf_str}")
            print("      draft:")
            for line in (getattr(resp, "draft", "") or "").splitlines():
                print(f"        {line}")
        except Exception as e:
            failed += 1
            print(f"      (draft FAILED: {type(e).__name__}: {e})")
        print()

    print(f"━━━ Summary: kept={kept}  skipped={skipped}  drafted={drafted}  failed={failed} ━━━")
    return 0


if __name__ == "__main__":
    sys.exit(main())
