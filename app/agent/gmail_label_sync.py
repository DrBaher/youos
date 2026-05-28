"""Gmail-label → dismissal sync.

Final piece of the remote-access series. /triage requires Tailscale; the
daily digest gives visibility but not interaction. This module gives the
user a way to **dismiss queued rows from any Gmail client** (phone, web,
work laptop) by applying a Gmail label to the original thread.

Convention:

  * **Label name**: ``YouOS/skip`` (the user creates this once in Gmail
    Labels settings; the prefix ``YouOS/`` is conventional and can be
    customised via ``agent.gmail_label_prefix`` if needed).
  * **Effect**: apply ``YouOS/skip`` to a thread that's pending in
    ``agent_pending_drafts`` → next sweep marks the row dismissed with
    ``reason='noise'`` (the dominant case for label-based dismissal).
  * **Idempotency**: the label is *removed* after processing, so a
    subsequent sweep doesn't re-process. Also, the row's status is
    checked — only ``pending`` or ``amended`` rows are dismissed; a
    row already ``sent`` or ``dismissed`` is skipped.

Per the b47 lesson, the gog command shape is verified live and the
single invocation is isolated in ``_gog_modify_remove_label`` for easy
correction.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Iterable

from app.agent import store

logger = logging.getLogger(__name__)

# The default label users apply to a thread to signal "stop drafting for
# this — it's noise." Configurable via agent.gmail_label_prefix.
DEFAULT_LABEL = "YouOS/skip"
GOG_TIMEOUT_SECONDS = 30


@dataclass
class LabelSyncResult:
    """Outcome of one ``sync_gmail_label_dismissals`` call.

    ``dismissed`` is the list of agent_pending_drafts row IDs that got
    marked dismissed-as-noise this sync. ``skipped`` lists threads that
    matched the label but couldn't be acted on (no pending row, or row
    already in a terminal state). ``errors`` captures per-thread errors
    so a single transient gog failure doesn't abort the whole sync.
    """

    dismissed: list[int]
    skipped: list[str]   # thread_ids that didn't map to actionable rows
    errors: list[str]


def sync_gmail_label_dismissals(
    *,
    account: str,
    database_url: str,
    label: str = DEFAULT_LABEL,
) -> LabelSyncResult:
    """Find Gmail threads tagged with ``label`` and dismiss their pending rows.

    Approach:
      1. Search Gmail for ``label:<label>`` via gog.
      2. For each thread, look up the matching ``agent_pending_drafts.thread_id``.
      3. If a pending/amended row exists, mark it dismissed with reason='noise'.
      4. Remove the label from the thread so we don't reprocess.

    Failures are isolated per-thread — one auth error doesn't abort the
    rest. Returns counts the caller (run_triage or the CLI) can log.
    """
    matched = _gog_search_labelled(account=account, label=label)
    dismissed_ids: list[int] = []
    skipped: list[str] = []
    errors: list[str] = []

    for entry in matched:
        thread_id = entry.get("threadId")
        message_id = entry.get("id")
        if not thread_id or not message_id:
            continue
        try:
            row = _find_pending_row_for_thread(database_url, account=account, thread_id=thread_id)
            if row is None:
                skipped.append(thread_id)
                continue
            store.mark_dismissed(database_url, row["id"], reason="noise")
            dismissed_ids.append(row["id"])
            # Remove the label so the next sync doesn't re-fire.
            try:
                _gog_modify_remove_label(account=account, message_id=message_id, label=label)
            except Exception as exc:
                # Don't roll back the dismissal — the user signalled intent.
                # Just log the cleanup failure so they can investigate.
                logger.warning(
                    "label-sync: dismissed row %s but failed to remove %r from thread %s: %s",
                    row["id"], label, thread_id, exc,
                )
        except Exception as exc:
            errors.append(f"thread {thread_id}: {exc}")

    if dismissed_ids:
        logger.info(
            "gmail-label sync (account=%s, label=%r): dismissed %d row(s) %s",
            account, label, len(dismissed_ids), dismissed_ids,
        )
    return LabelSyncResult(
        dismissed=dismissed_ids,
        skipped=skipped,
        errors=errors,
    )


# --- DB lookup -------------------------------------------------------------


def _find_pending_row_for_thread(
    database_url: str, *, account: str, thread_id: str,
) -> dict | None:
    """Find the (only) actionable row for a given Gmail thread.

    Returns the highest-id pending/amended row for the thread+account pair,
    or None. The 'highest id' tiebreaker matters if the same thread came
    through multiple sweeps and produced multiple rows (rare but possible
    when message_id changes between sweeps for the same thread).
    """
    import sqlite3

    # Use the same removeprefix path as the rest of the agent module —
    # b49 lesson, urllib.parse absolutizes sqlite:/// relative paths.
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Only sqlite:/// URLs are supported (got {database_url!r})")
    db_path = database_url.removeprefix(prefix)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT * FROM agent_pending_drafts
               WHERE account = ? AND thread_id = ? AND status IN ('pending', 'amended')
               ORDER BY id DESC LIMIT 1""",
            (account, thread_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# --- gog wire-shape (verified b57) -----------------------------------------


def _gog_search_labelled(*, account: str, label: str) -> list[dict]:
    """List messages tagged with ``label`` for ``account``.

    Uses ``gog gmail search 'label:<label>'`` — verified shape b57 (alias
    of b34's ingestion search path, same flags). Returns the parsed
    threads/messages list; empty list on no match.
    """
    cmd = [
        "gog", "gmail", "search", f"label:{label}",
        "--account", account,
        "--json", "--no-input",
        "--max", "200",       # bound the per-sync workload
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise RuntimeError("gog CLI not on PATH; cannot sync Gmail labels") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gog gmail search timed out ({GOG_TIMEOUT_SECONDS}s)") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Label-not-yet-created is normal (user hasn't tagged anything yet);
        # we don't want this to ERROR the run. Detect the common gog message
        # for invalid query and silently return empty.
        if "invalid label" in stderr.lower() or "label not found" in stderr.lower():
            return []
        raise RuntimeError(f"gog returned exit {result.returncode}: {stderr or 'no stderr'}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gog returned non-JSON: {result.stdout[:200]!r}") from exc
    # Envelope-style — search returns {"threads":[...], "nextPageToken":...}
    if isinstance(payload, dict):
        return list(payload.get("threads") or payload.get("messages") or [])
    return list(payload) if isinstance(payload, list) else []


def _gog_modify_remove_label(*, account: str, message_id: str, label: str) -> None:
    """Remove a label from a single Gmail message.

    Verified b57: ``gog gmail messages modify <id> --account X --remove <label>
    --no-input --json``. The ``--remove`` flag takes a comma-separated list
    but a single label is fine. Errors raise; caller decides whether to
    propagate.
    """
    cmd = [
        "gog", "gmail", "messages", "modify", message_id,
        "--account", account,
        "--remove", label,
        "--json", "--no-input",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT_SECONDS)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"gog modify --remove failed: {stderr or 'no stderr'}")


def matched_thread_ids(matched: Iterable[dict]) -> list[str]:
    """Test helper — extract thread IDs from a gog search payload."""
    return [m.get("threadId") for m in matched if m.get("threadId")]
