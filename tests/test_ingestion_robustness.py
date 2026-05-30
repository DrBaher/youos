"""Ingestion robustness — PR #13 deferred items.

PR #13 ("ingestion + CLI review") fixed the critical ingestion bugs but
flagged three follow-ups: WhatsApp tz-naive timestamps, WhatsApp minute-
resolution `source_id` collisions, and Gmail rate-limit/backoff. Each is
small and bounded on its own, but each is a real per-event bug.

## WhatsApp tz

`_parse_timestamp` used to call `datetime.strptime(...).isoformat()`,
producing tz-naive ISO strings like `2026-03-14T15:30:00`. Stored that
way in `documents.created_at` and `reply_pairs.paired_at`, so a
recency-boost or time-window query interleaved WhatsApp pairs with Gmail
pairs incorrectly (Gmail pairs are tz-aware via RFC 2822). Now: attach
`user.timezone` (or UTC fallback) before isoformat.

## WhatsApp source_id collisions

WhatsApp export timestamps have **minute** resolution, so two messages in
the same minute from the same sender produced identical
`source_id = wa-{chat}-{ts}-{sender}` and the second was silently dropped
by INSERT OR IGNORE. Now the `source_id` includes an 8-char hash of
content + sender — same content → same id (idempotent re-ingest); distinct
content → distinct id (no silent drop). Same fingerprint added to
`reply_pairs.source_id` to handle the same-minute-inbound + same-minute-
reply case.

## Gmail rate-limit/backoff

`_run_gog_json` used to raise on any returncode != 0, including transient
quota/rate-limit responses. A single 429 mid-sync would abort the whole
ingestion run. Now: stderr/stdout is inspected for Google's quota-exceeded
wording (`429`, `rateLimitExceeded`, `quotaExceeded`, `Too Many Requests`,
…); on a match we wait 2 → 4 → 8 → 16 seconds (5 attempts total, ~30s
max wait) before re-raising. Non-rate-limit errors still raise on first
hit. Search-page subprocess call routes through the same helper so
search-page 429s get the same retry treatment.
"""

from __future__ import annotations

import re
import subprocess
import time
from unittest.mock import patch

import pytest

# ── WhatsApp: tz-aware timestamps ─────────────────────────────────────────

def test_parse_timestamp_default_returns_tz_aware(monkeypatch):
    """No `tz_name` arg → reads `user.timezone` from config. With no
    config available the helper falls back to UTC — the persisted ISO
    string must always carry an offset, never be naive."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **kw: {})
    from app.ingestion.whatsapp import _parse_timestamp

    result = _parse_timestamp("3/14/24, 3:30 PM")
    assert result is not None
    # Naive ISO would end with `00:00` and no offset. Tz-aware must have +OFFSET.
    assert re.search(r"[+-]\d{2}:\d{2}$", result), f"expected offset suffix, got {result!r}"


def test_parse_timestamp_honors_user_timezone():
    """Explicit `tz_name` arg → that zone wins."""
    from app.ingestion.whatsapp import _parse_timestamp

    iso = _parse_timestamp("3/14/24, 3:30 PM", tz_name="Europe/Vienna")
    assert iso is not None
    # Vienna in March is UTC+1 (standard) or UTC+2 (DST). Either is fine; the
    # point is the offset is *non-UTC*, proving the zone applied.
    assert iso.startswith("2024-03-14T15:30:00")
    assert iso.endswith("+01:00") or iso.endswith("+02:00")


def test_parse_timestamp_falls_back_to_utc_on_unknown_tz():
    """A typo in the config (`Europe/Vienn`) used to be ignored at the
    config layer; now we fall through to UTC rather than crashing the
    whole WhatsApp ingest."""
    from app.ingestion.whatsapp import _parse_timestamp

    iso = _parse_timestamp("3/14/24, 3:30 PM", tz_name="Europe/Definitely-Not-A-Zone")
    assert iso == "2024-03-14T15:30:00+00:00"


def test_parse_timestamp_returns_none_for_unparseable_input():
    """Unchanged behaviour for inputs that don't match either format."""
    from app.ingestion.whatsapp import _parse_timestamp

    assert _parse_timestamp("not a date", tz_name="UTC") is None


def test_parse_timestamp_handles_both_y_and_Y_formats():
    """WhatsApp exports vary by export date — older use 2-digit year,
    newer use 4-digit. Both must produce tz-aware output."""
    from app.ingestion.whatsapp import _parse_timestamp

    short_year = _parse_timestamp("3/14/24, 3:30 PM", tz_name="UTC")
    long_year = _parse_timestamp("3/14/2024, 3:30 PM", tz_name="UTC")
    assert short_year == long_year == "2024-03-14T15:30:00+00:00"


# ── WhatsApp: content fingerprint disambiguates collisions ────────────────

def test_content_fingerprint_stable_for_same_inputs():
    """Re-ingesting the same export must produce identical source_ids so
    INSERT OR IGNORE actually IGNOREs (idempotent re-ingestion)."""
    from app.ingestion.whatsapp import _content_fingerprint

    a = _content_fingerprint("Hello there", "Alice")
    b = _content_fingerprint("Hello there", "Alice")
    assert a == b


def test_content_fingerprint_differs_for_different_content():
    """The whole point: two messages in the same minute from the same
    sender used to collide. Distinct content must now produce distinct
    fingerprints so both get persisted."""
    from app.ingestion.whatsapp import _content_fingerprint

    a = _content_fingerprint("Hello", "Alice")
    b = _content_fingerprint("Goodbye", "Alice")
    assert a != b


def test_content_fingerprint_differs_for_different_senders():
    """Edge case: same text from two senders in same minute. (Less likely
    than same-sender bursts but cheap to disambiguate.)"""
    from app.ingestion.whatsapp import _content_fingerprint

    a = _content_fingerprint("ack", "Alice")
    b = _content_fingerprint("ack", "Bob")
    assert a != b


def test_content_fingerprint_is_short_and_hex():
    """8 chars of hex is plenty for collision avoidance within a single
    chat and keeps source_id readable in DB inspections."""
    from app.ingestion.whatsapp import _content_fingerprint

    fp = _content_fingerprint("hello", "Alice")
    assert len(fp) == 8
    assert all(c in "0123456789abcdef" for c in fp)


# ── WhatsApp: end-to-end no-drop on same-minute burst ────────────────────

@pytest.fixture
def _wa_export_with_collision(tmp_path):
    """Export with two distinct messages in the same minute from the same
    sender — the exact pattern that used to silently lose one to
    INSERT OR IGNORE."""
    export = tmp_path / "chat.txt"
    export.write_text(
        "3/14/24, 3:30 PM - Alice: First message\n"
        "3/14/24, 3:30 PM - Alice: Second message different content\n"
        "3/14/24, 3:31 PM - You: ack to both\n",
        encoding="utf-8",
    )
    return export


def test_ingest_does_not_drop_same_minute_messages(monkeypatch, _wa_export_with_collision, tmp_path):
    """Regression: both Alice messages must end up in `documents`,
    not just the first.

    We patch the run-log helpers (which require the full schema's
    `ingest_runs` table) to no-ops; this test cares only about
    document/reply_pair INSERT behaviour, which is where the
    collision bug lived. A separate full-schema E2E test would be
    duplicating the existing ingestion-imports test coverage.
    """
    import sqlite3

    from app.ingestion import whatsapp as wa

    monkeypatch.setattr(wa, "start_ingest_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(wa, "finish_ingest_run", lambda *_a, **_kw: None)

    db = tmp_path / "youos.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            source_type TEXT,
            source_id TEXT UNIQUE,
            title TEXT,
            author TEXT,
            content TEXT,
            metadata_json TEXT,
            ingestion_run_id TEXT,
            created_at TEXT
        );
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            source_type TEXT,
            source_id TEXT UNIQUE,
            document_id INTEGER,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            paired_at TEXT,
            metadata_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    result = wa.ingest_whatsapp_export(_wa_export_with_collision, db_path=db, user_names=("You",))
    assert result.status == "completed"

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT source_id, content FROM documents WHERE source_type = 'whatsapp_export' ORDER BY id"
    ).fetchall()
    conn.close()

    # Both Alice messages must be present — pre-fix only the first survived.
    contents = [r[1] for r in rows]
    assert "First message" in contents
    assert "Second message different content" in contents

    # And their source_ids must be distinct (the fingerprint suffix did
    # its job).
    source_ids = [r[0] for r in rows]
    assert len(source_ids) == len(set(source_ids)), f"duplicate source_ids: {source_ids}"


# ── Gmail: rate-limit detection + backoff ────────────────────────────────

def test_looks_like_rate_limit_matches_known_patterns():
    from app.ingestion.gmail_threads import _looks_like_rate_limit

    assert _looks_like_rate_limit("Error 429: Too Many Requests")
    assert _looks_like_rate_limit("rateLimitExceeded for user")
    assert _looks_like_rate_limit("userRateLimitExceeded: quota of 60/min")
    assert _looks_like_rate_limit("quotaExceeded for project")
    assert _looks_like_rate_limit("rate limit hit")
    assert _looks_like_rate_limit("quota exceeded for daily")


def test_looks_like_rate_limit_is_case_insensitive():
    from app.ingestion.gmail_threads import _looks_like_rate_limit

    assert _looks_like_rate_limit("RATELIMITEXCEEDED")


def test_looks_like_rate_limit_rejects_unrelated_errors():
    """A network timeout or a malformed-thread error must NOT trigger
    retry — those won't clear no matter how long we wait."""
    from app.ingestion.gmail_threads import _looks_like_rate_limit

    assert not _looks_like_rate_limit("connection refused")
    assert not _looks_like_rate_limit("thread payload malformed")
    assert not _looks_like_rate_limit("permission denied")
    assert not _looks_like_rate_limit("")


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gog"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_gog_json_returns_payload_on_first_success(monkeypatch):
    """No retries on success — the happy path stays single-shot."""
    from app.ingestion import gmail_threads as gt

    call_count = {"n": 0}

    def _fake_run(*a, **kw):
        call_count["n"] += 1
        return _completed(0, stdout='{"ok": true}')

    monkeypatch.setattr(gt.subprocess, "run", _fake_run)
    monkeypatch.setattr(gt.time, "sleep", lambda _s: None)

    payload = gt._run_gog_json(["gog", "test"])
    assert payload == {"ok": True}
    assert call_count["n"] == 1


def test_run_gog_json_retries_on_rate_limit_then_succeeds(monkeypatch):
    """The whole point: a 429 mid-pull should retry, not abort. Two 429s
    then a success → should return the success payload."""
    from app.ingestion import gmail_threads as gt

    seq = [
        _completed(1, stderr="Error 429: rateLimitExceeded"),
        _completed(1, stderr="Error 429: Too Many Requests"),
        _completed(0, stdout='{"ok": true}'),
    ]

    def _fake_run(*a, **kw):
        return seq.pop(0)

    monkeypatch.setattr(gt.subprocess, "run", _fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(gt.time, "sleep", lambda s: sleeps.append(s))

    payload = gt._run_gog_json(["gog", "test"])
    assert payload == {"ok": True}
    # Two retries → two backoff waits (2s, 4s — start of the backoff schedule).
    assert sleeps == [2, 4]


def test_run_gog_json_does_not_retry_non_rate_limit_errors(monkeypatch):
    """Distinct from "transient" failure modes — auth errors, missing
    files, malformed payloads should fail-fast, not absorb 30s of backoff
    waiting for a quota to clear that has nothing to do with the bug."""
    from app.ingestion import gmail_threads as gt

    call_count = {"n": 0}

    def _fake_run(*a, **kw):
        call_count["n"] += 1
        return _completed(1, stderr="auth credentials invalid")

    monkeypatch.setattr(gt.subprocess, "run", _fake_run)
    monkeypatch.setattr(gt.time, "sleep", lambda _s: None)

    with pytest.raises(ValueError, match="auth credentials invalid"):
        gt._run_gog_json(["gog", "test"])
    assert call_count["n"] == 1


def test_run_gog_json_gives_up_after_backoff_budget(monkeypatch):
    """Sustained outage: after 4 retries (2+4+8+16=30s) we give up and
    raise so the nightly's overall timeout still kicks in. Without this
    cap, infinite retry could pin the nightly forever."""
    from app.ingestion import gmail_threads as gt

    call_count = {"n": 0}

    def _fake_run(*a, **kw):
        call_count["n"] += 1
        return _completed(1, stderr="quotaExceeded sustained")

    monkeypatch.setattr(gt.subprocess, "run", _fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(gt.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ValueError, match="quotaExceeded"):
        gt._run_gog_json(["gog", "test"])
    # 5 attempts total (1 initial + 4 retries).
    assert call_count["n"] == 5
    assert sleeps == [2, 4, 8, 16]


def test_run_gog_json_raises_on_invalid_json_without_retry(monkeypatch):
    """A successful return-code with malformed JSON isn't a rate-limit
    condition; bubble immediately rather than absorbing backoff waits."""
    from app.ingestion import gmail_threads as gt

    def _fake_run(*a, **kw):
        return _completed(0, stdout="not json at all{")

    monkeypatch.setattr(gt.subprocess, "run", _fake_run)
    monkeypatch.setattr(gt.time, "sleep", lambda _s: None)

    with pytest.raises(ValueError, match="invalid JSON"):
        gt._run_gog_json(["gog", "test"])


def test_run_gog_json_propagates_subprocess_timeout(monkeypatch):
    """`TimeoutExpired` (per-call timeout, not per-bunch backoff) keeps
    its existing behaviour — don't silently retry past `GOG_TIMEOUT_SECONDS`."""
    from app.ingestion import gmail_threads as gt

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["gog"], timeout=120)

    monkeypatch.setattr(gt.subprocess, "run", _raise_timeout)
    monkeypatch.setattr(gt.time, "sleep", lambda _s: None)

    with pytest.raises(ValueError, match="timed out"):
        gt._run_gog_json(["gog", "test"])


# Silence pytest-unused lint warnings for now.
_ = (patch, time)


# --- b133: ingest crash-isolation (paren bomb, deep MIME, caps, per-item) ----


def test_addresses_paren_bomb_degrades_not_recursionerror():
    from app.ingestion.gmail_threads import _addresses_from_text, _safe_parseaddr

    bomb = "((((" * 1000 + "a@x.com" + "))))" * 1000
    assert _addresses_from_text(bomb) == []        # was RecursionError -> aborted the run
    assert _safe_parseaddr(bomb) == ("", "")
    # legit headers still parse
    assert [r["email"] for r in _addresses_from_text("Alice <a@x.com>, Bob <b@y.com>")] == ["a@x.com", "b@y.com"]
    assert _safe_parseaddr("a@x.com (Alice)")[1] == "a@x.com"


def test_extract_payload_parts_is_depth_bounded():
    from app.ingestion.gmail_threads import _extract_payload_parts

    node = {"mimeType": "text/plain", "body": {"data": "aGk="}}
    for _ in range(6000):
        node = {"mimeType": "multipart/mixed", "parts": [node]}
    assert _extract_payload_parts(node, mime_type="text/plain") == []  # past cap, no RecursionError
    shallow = {"mimeType": "multipart/mixed",
               "parts": [{"mimeType": "text/plain", "body": {"data": "aGVsbG8="}}]}
    assert _extract_payload_parts(shallow, mime_type="text/plain") == ["hello"]


def test_message_body_text_is_capped():
    from app.ingestion.gmail_threads import _MAX_BODY_CHARS, _message_body_text

    assert len(_message_body_text({"body_text": "X" * (_MAX_BODY_CHARS + 50_000)})) == _MAX_BODY_CHARS


def test_normalize_email_rejects_dash_leading():
    from app.ingestion.gmail_threads import _normalize_email

    assert _normalize_email("-x@evil.com") is None     # gog --to flag-injection shape
    assert _normalize_email("  A@X.com ") == "a@x.com"


def test_load_thread_payloads_skips_corrupt_file(tmp_path):
    import json as _json

    from app.ingestion.gmail_threads import _load_thread_payloads

    (tmp_path / "good.json").write_text(_json.dumps({"thread_id": "t1", "messages": []}))
    (tmp_path / "bad.json").write_text("{not valid json")
    result = _load_thread_payloads(tmp_path, live=None)
    assert len(result.payloads) == 1  # corrupt file skipped, good thread still loaded


def test_whatsapp_refuses_oversize_export(tmp_path, monkeypatch):
    from app.ingestion import whatsapp as wa

    f = tmp_path / "chat.txt"
    f.write_text("hello there")
    monkeypatch.setattr(wa, "_MAX_EXPORT_BYTES", 1)
    r = wa.ingest_whatsapp_export(f)
    assert r.status == "failed" and "too large" in r.detail.lower()
