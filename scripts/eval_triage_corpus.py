#!/usr/bin/env python3
"""Re-evaluate the live triage queue corpus with the CURRENT classifier.

Unlike ``scripts/eval_triage.py`` (a labelled JSONL corpus), this re-runs
``needs_reply.classify`` over every row already in ``agent_pending_drafts`` — the
real, already-processed corpus — to measure what the current rules would now do,
with the real sender history loaded (so established correspondents aren't treated
as first-contact).

The fidelity catch it fixes: the stored rows persist only ``to``/``cc``
recipients, NOT ``List-Id`` / ``List-Unsubscribe``. Re-classifying them therefore
can't apply the list-mail HARD SKIPS, so bulk newsletters look like drafts (an
eval artifact, not a prod bug — in prod the live headers are present). With
``--refetch-drafts`` (default), each row that lands in the DRAFT tier is
re-fetched live (``inbox_fetch.fetch_thread`` returns ALL headers) and
re-classified, so newsletters with a List-* header correctly fall to skip. Rows
whose thread is gone keep their stored-header verdict.

Usage:
    python scripts/eval_triage_corpus.py [--db sqlite:///…] [--no-refetch]
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _tier(v) -> str:
    return "draft" if v.needs_reply else ("surface" if v.surface_for_review else "skip")


def main() -> None:
    from app.agent.inbox_fetch import InboxMessage, fetch_thread
    from app.agent.needs_reply import SenderHistory, classify
    from app.agent.scheduler import get_agent_config
    from app.core.config import get_user_emails
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    parser = argparse.ArgumentParser(description="Re-evaluate the triage queue corpus")
    parser.add_argument("--db", default=None, help="sqlite:/// URL (default: settings)")
    parser.add_argument("--no-refetch", action="store_true",
                        help="skip re-fetching live headers for draft-tier rows (faster, but List-* artifact returns)")
    args = parser.parse_args()

    settings = get_settings()
    db_url = args.db or settings.database_url
    cfg = get_agent_config()
    skip = cfg.get("skip_senders") or []
    vip = cfg.get("vip_senders") or []
    ue = [e.lower() for e in get_user_emails()]
    thr = float(cfg.get("threshold", 0.6) or 0.6)
    history = SenderHistory.from_database_url(db_url)
    refetch = not args.no_refetch

    conn = sqlite3.connect(resolve_sqlite_path(db_url))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT account, message_id, thread_id, sender, sender_email, subject, body, "
        "to_recipients, cc_recipients FROM agent_pending_drafts"
    ).fetchall()

    counts: collections.Counter[str] = collections.Counter()
    noreply_drafting = 0
    cold_demoted = 0
    corrected: list[tuple[str, str, str]] = []  # (sender, subject, hard-skip reason)

    for r in rows:
        own = {r["account"].lower(), *ue}
        headers: dict[str, str] = {}
        if r["to_recipients"]:
            headers["to"] = r["to_recipients"]
        if r["cc_recipients"]:
            headers["cc"] = r["cc_recipients"]
        msg = InboxMessage(
            message_id=r["message_id"] or "", thread_id=r["thread_id"] or "", account=r["account"],
            sender=r["sender"] or "", sender_email=r["sender_email"], subject=r["subject"] or "",
            body=r["body"] or "", headers=headers, received_at=None, has_attachment=False,
        )
        v = classify(msg, history=history, threshold=thr, skip_senders=skip, vip_senders=vip, account_emails=list(own))
        tier = _tier(v)

        # Artifact fix: a draft-tier row may carry a List-* header we didn't
        # persist. Re-fetch the live thread (real headers) and re-classify.
        if tier == "draft" and refetch and r["thread_id"]:
            try:
                live = fetch_thread(r["account"], r["thread_id"])
            except Exception:
                live = None
            if live is not None:
                v2 = classify(live, history=history, threshold=thr, skip_senders=skip, vip_senders=vip, account_emails=list(own))
                t2 = _tier(v2)
                if t2 != "draft":
                    corrected.append((r["sender"] or "", r["subject"] or "", "; ".join(v2.reasons)[:60]))
                    v, tier = v2, t2

        counts[tier] += 1
        if tier == "draft" and any("noreply" in x for x in v.reasons):
            noreply_drafting += 1
        if any("cold pitch" in x for x in v.reasons):
            cold_demoted += 1

    total = sum(counts.values())
    print(f"=== Triage queue corpus eval — {total} real processed emails ===")
    print(f"  refetch live headers for draft tier: {refetch}\n")
    print(f"  would-draft : {counts['draft']}")
    print(f"  surface     : {counts['surface']}")
    print(f"  hard-skip   : {counts['skip']}\n")
    print(f"  noreply senders drafting     : {noreply_drafting}  (target 0)")
    print(f"  confident cold pitches demoted: {cold_demoted}")
    print(f"  draft-tier rows corrected by live headers (List-* etc.): {len(corrected)}")
    for sender, subject, reason in corrected:
        print(f"    • {sender[:30]:30} | {subject[:42]:42} | {reason}")


if __name__ == "__main__":
    main()
