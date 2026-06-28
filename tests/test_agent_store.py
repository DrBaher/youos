"""DAL for the agent_pending_drafts table — insert idempotency, list filters,
state transitions (amend / sent / dismissed)."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


_DEFAULTS = dict(
    message_id="m-1",
    thread_id="t-1",
    account="you@example.com",
    sender="Alice <alice@partner.com>",
    sender_email="alice@partner.com",
    subject="Pricing",
    body="Q3 pricing?",
    received_at="2026-05-28T10:00:00Z",
    needs_reply_score=0.75,
    reasons=["ends with a question", "imperative verb present"],
    cold_outreach=False,
    tier="draft",
    draft="Confirmed — pricing unchanged.",
    draft_model="qwen2.5-1.5b-lora",
    draft_repairs=["stripped_trailing_signature"],
    standing_instructions_snapshot=None,
)


def test_upsert_inserts_then_ignores_duplicates(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert isinstance(row_id, int) and row_id > 0
    again = store.upsert_pending(db_url, **_DEFAULTS)
    assert again is None  # idempotent — same message_id


def test_list_pending_returns_newest_first_and_rehydrates_json(db_url):
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-1", "needs_reply_score": 0.65})
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2", "needs_reply_score": 0.85})
    rows = store.list_pending(db_url)
    assert len(rows) == 2
    # Sorted by score DESC then created_at — higher-confidence draft first.
    assert rows[0]["message_id"] == "m-2"
    # JSON columns rehydrated to lists.
    assert rows[0]["reasons"] == _DEFAULTS["reasons"]
    assert rows[0]["draft_repairs"] == _DEFAULTS["draft_repairs"]
    assert rows[0]["cold_outreach"] is False


def test_get_by_thread_returns_latest_for_thread(db_url):
    """b280: the Gmail Add-on looks up YouOS's row by the open thread's Gmail id.
    Returns the most recent row for the thread (id-tiebreak when same second),
    None for an unknown thread."""
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "ma", "thread_id": "tA"})
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "mb", "thread_id": "tA"})  # newer, same thread
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "mc", "thread_id": "tB"})

    a = store.get_by_thread(db_url, "tA")
    assert a is not None and a["thread_id"] == "tA"
    assert a["message_id"] == "mb"  # the most recent row for the thread
    assert store.get_by_thread(db_url, "tB")["message_id"] == "mc"
    assert store.get_by_thread(db_url, "no-such-thread") is None
    assert store.get_by_thread(db_url, "") is None


def test_list_pending_filters_by_tier(db_url):
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-1", "tier": "draft"})
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2", "tier": "surface", "draft": None})
    drafts = store.list_pending(db_url, tier="draft")
    surface = store.list_pending(db_url, tier="surface")
    assert {r["message_id"] for r in drafts} == {"m-1"}
    assert {r["message_id"] for r in surface} == {"m-2"}


def test_state_transitions_amend_then_send(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_amended(db_url, row_id, amended_draft="Confirmed — pricing held.")
    r = store.get(db_url, row_id)
    assert r["status"] == "amended"
    assert r["amended_draft"] == "Confirmed — pricing held."

    assert store.mark_sent(db_url, row_id)
    r = store.get(db_url, row_id)
    assert r["status"] == "sent"
    assert r["sent_at"] is not None


def test_dismiss_marks_dismissed(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id)
    r = store.get(db_url, row_id)
    assert r["status"] == "dismissed"
    assert r["dismissed_at"] is not None
    # No reason supplied — column stays NULL ("no_reason" bucket in stats).
    assert r["dismissal_reason"] is None


def test_dismiss_records_categorical_reason(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id, reason="noise")
    r = store.get(db_url, row_id)
    assert r["dismissal_reason"] == "noise"


def test_dismiss_coerces_unknown_reason_to_other(db_url):
    """Defence in depth — API rejects unknown reasons upstream, but if a
    legacy caller passes something we don't recognise the DAL keeps the
    column bounded by mapping it to 'other'."""
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id, reason="some_random_bucket")
    r = store.get(db_url, row_id)
    assert r["dismissal_reason"] == "other"


def test_dismissal_stats_aggregates_by_reason(db_url):
    from app.agent import store

    # 4 rows: 2 dismissed (noise + wrong_sender), 1 dismissed no-reason, 1 pending.
    for i, (reason, dismiss) in enumerate([
        ("noise", True),
        ("wrong_sender", True),
        (None, True),
        (None, False),
    ]):
        rid = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": f"m-{i}"})
        if dismiss:
            store.mark_dismissed(db_url, rid, reason=reason)

    stats = store.dismissal_stats(db_url)
    assert stats["total_persisted"] == 4
    assert stats["dismissed"] == 3
    assert stats["dismissal_rate"] == 0.75
    assert stats["by_reason"]["noise"] == 1
    assert stats["by_reason"]["wrong_sender"] == 1
    assert stats["by_reason"]["no_reason"] == 1
    assert stats["by_reason"]["wrong_content"] == 0  # zero-filled


def test_dismissal_stats_filters_by_account(db_url):
    from app.agent import store

    a = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-a", "account": "a@x.com"})
    b = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-b", "account": "b@x.com"})
    store.mark_dismissed(db_url, a, reason="noise")
    store.mark_dismissed(db_url, b, reason="wrong_content")

    only_a = store.dismissal_stats(db_url, account="a@x.com")
    assert only_a["total_persisted"] == 1
    assert only_a["dismissed"] == 1
    assert only_a["by_reason"]["noise"] == 1
    assert only_a["by_reason"]["wrong_content"] == 0


def test_list_pending_default_excludes_non_pending(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2"})
    store.mark_dismissed(db_url, row_id)
    rows = store.list_pending(db_url)  # default status='pending'
    assert {r["message_id"] for r in rows} == {"m-2"}


# --- ε: audit log ---------------------------------------------------------


def test_log_sweep_records_auto_promoted_senders(db_url):
    """b52: the audit row captures which senders were auto-added to
    skip_senders during this sweep — rehydrated on list_recent_sweeps."""
    from app.agent import store

    store.log_sweep(
        db_url, account="a@x.com", trigger="scheduled", window="24h", threshold=0.6,
        fetched=8, kept=2, surfaced=1, persisted=2, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:01Z",
        duration_ms=1000,
        auto_promoted_senders=["spam@x.com", "noise@y.com"],
    )
    sweeps = store.list_recent_sweeps(db_url)
    assert len(sweeps) == 1
    # JSON column rehydrated to a list under the bare key.
    assert sweeps[0]["auto_promoted"] == ["spam@x.com", "noise@y.com"]


def test_log_sweep_auto_promoted_defaults_to_empty_list(db_url):
    """Callers that omit ``auto_promoted_senders`` get an empty list back —
    matches the column default and keeps the UI null-safe."""
    from app.agent import store

    store.log_sweep(
        db_url, account="a@x.com", trigger="manual", window=None, threshold=None,
        fetched=0, kept=0, surfaced=0, persisted=0, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:01Z",
        duration_ms=500,
    )
    sweeps = store.list_recent_sweeps(db_url)
    assert sweeps[0]["auto_promoted"] == []


def test_log_sweep_inserts_and_list_recent_orders_newest_first(db_url):
    from app.agent import store

    store.log_sweep(
        db_url, account="a@x.com", trigger="manual", window="3d", threshold=0.6,
        fetched=5, kept=2, surfaced=1, persisted=2, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:02Z",
        duration_ms=2000,
    )
    store.log_sweep(
        db_url, account="a@x.com", trigger="scheduled", window="24h", threshold=0.6,
        fetched=12, kept=0, surfaced=2, persisted=0,
        errors=["gog auth failed"], standing_instructions_snapshot="be brief",
        started_at="2026-05-28T11:00:00Z", finished_at="2026-05-28T11:00:01Z",
        duration_ms=1000,
    )

    sweeps = store.list_recent_sweeps(db_url)
    assert len(sweeps) == 2
    # Newest first.
    assert sweeps[0]["trigger"] == "scheduled"
    # JSON column rehydrated.
    assert sweeps[0]["errors"] == ["gog auth failed"]
    assert sweeps[1]["errors"] == []


def test_list_recent_sweeps_filters_by_account(db_url):
    from app.agent import store

    for acct in ("a@x.com", "b@y.com", "a@x.com"):
        store.log_sweep(
            db_url, account=acct, trigger="manual", window="3d", threshold=0.6,
            fetched=0, kept=0, surfaced=0, persisted=0, errors=[],
            standing_instructions_snapshot=None,
            started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:01Z",
            duration_ms=1000,
        )
    only_a = store.list_recent_sweeps(db_url, account="a@x.com")
    assert len(only_a) == 2
    assert all(s["account"] == "a@x.com" for s in only_a)


# --- b42: observability aggregates ---------------------------------------


def _log(db_url, **overrides):
    """Test helper — log a sweep with sensible defaults, override what matters."""
    from datetime import datetime, timezone

    from app.agent import store

    # Use a CURRENT timestamp, not a hardcoded date: sweep_aggregate() filters to
    # the last 30 days, so a fixed past date (was "2026-05-28") silently ages out
    # of the window and turns these into time-bombs that fail once the clock
    # passes that date + 30d.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    defaults = dict(
        account="a@x.com", trigger="manual", window="3d", threshold=0.6,
        fetched=0, kept=0, surfaced=0, persisted=0, errors=[],
        standing_instructions_snapshot=None,
        started_at=now, finished_at=now,
        duration_ms=1000,
    )
    defaults.update(overrides)
    store.log_sweep(db_url, **defaults)


def test_sweep_aggregate_sums_counters_and_computes_success_rate(db_url):
    from app.agent import store

    # 3 sweeps: 2 successful (empty errors), 1 failed.
    _log(db_url, fetched=10, kept=4, surfaced=1, persisted=4, errors=[])
    _log(db_url, fetched=8,  kept=3, surfaced=2, persisted=3, errors=[])
    _log(db_url, fetched=12, kept=5, surfaced=0, persisted=5, errors=["gog auth"])

    agg = store.sweep_aggregate(db_url)
    assert agg["sweeps"] == 3
    assert agg["successful"] == 2
    assert agg["success_rate"] == round(2/3, 4)
    assert agg["fetched"] == 30
    assert agg["kept"] == 12
    assert agg["surfaced"] == 3
    assert agg["persisted"] == 12
    assert agg["hard_skipped"] == 30 - 12   # fetched - kept


def test_sweep_aggregate_filters_by_account(db_url):
    from app.agent import store

    _log(db_url, account="a@x.com", fetched=10, kept=4)
    _log(db_url, account="b@x.com", fetched=20, kept=8)
    only_a = store.sweep_aggregate(db_url, account="a@x.com")
    assert only_a["fetched"] == 10
    assert only_a["kept"] == 4


def test_sweep_aggregate_empty_returns_zero_counts_and_zero_rate(db_url):
    from app.agent import store

    agg = store.sweep_aggregate(db_url)
    assert agg["sweeps"] == 0
    assert agg["success_rate"] == 0.0   # no sweeps → no rate to compute
    assert agg["fetched"] == 0


def test_score_histogram_buckets_by_score(db_url):
    from app.agent import store

    for i, score in enumerate([0.10, 0.40, 0.55, 0.75, 0.80, 0.95, 1.0]):
        store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": f"m-{i}", "needs_reply_score": score})

    h = store.score_histogram(db_url)["buckets"]
    # 0.10 → 0.0-0.3 ; 0.40 → 0.3-0.5 ; 0.55 → 0.5-0.7 ; 0.75 + 0.80 → 0.7-0.9 ; 0.95 + 1.0 → 0.9-1.0
    assert h["0.0-0.3"] == 1
    assert h["0.3-0.5"] == 1
    assert h["0.5-0.7"] == 1
    assert h["0.7-0.9"] == 2
    assert h["0.9-1.0"] == 2


# --- b43: skip-sender promotion candidates -----------------------------


def test_noise_candidates_groups_by_sender_and_filters_by_min_count(db_url):
    from app.agent import store

    # alice: 3 noise dismissals — should appear
    # bob:   1 noise dismissal  — below min_count
    # carol: 2 noise dismissals — should appear
    # dan:   2 wrong_content dismissals — wrong reason, should NOT appear
    for sender, reason, n in [
        ("alice@x.com", "noise", 3),
        ("bob@x.com",   "noise", 1),
        ("carol@x.com", "noise", 2),
        ("dan@x.com",   "wrong_content", 2),
    ]:
        for i in range(n):
            rid = store.upsert_pending(db_url, **{
                **_DEFAULTS,
                "message_id": f"{sender}-{i}",
                "sender_email": sender,
                "subject": f"Marketing blast {i}",
            })
            store.mark_dismissed(db_url, rid, reason=reason)

    cands = store.noise_dismissal_candidates(db_url, min_count=2)
    senders = {c["sender_email"] for c in cands}
    assert senders == {"alice@x.com", "carol@x.com"}
    # Ordered by count DESC.
    assert cands[0]["sender_email"] == "alice@x.com"
    assert cands[0]["count"] == 3
    assert cands[0]["last_subject"]  # populated, not None


def test_noise_candidates_lowercases_sender_for_grouping(db_url):
    from app.agent import store

    # Same sender, different casing — should group as one.
    for i, email in enumerate(["Alice@X.COM", "alice@x.com", "ALICE@x.com"]):
        rid = store.upsert_pending(db_url, **{
            **_DEFAULTS,
            "message_id": f"m-{i}",
            "sender_email": email,
        })
        store.mark_dismissed(db_url, rid, reason="noise")

    cands = store.noise_dismissal_candidates(db_url, min_count=2)
    assert len(cands) == 1
    assert cands[0]["sender_email"] == "alice@x.com"
    assert cands[0]["count"] == 3


def test_noise_candidates_skips_null_or_empty_sender_email(db_url):
    """Rows without a sender_email can't be added to skip_senders, so they
    have to be excluded from the candidates list (defence against a noisy
    fixture that drafted from anonymous senders)."""
    from app.agent import store

    for i, email in enumerate([None, "", "valid@x.com", "valid@x.com"]):
        rid = store.upsert_pending(db_url, **{
            **_DEFAULTS,
            "message_id": f"m-{i}",
            "sender_email": email,
        })
        store.mark_dismissed(db_url, rid, reason="noise")

    cands = store.noise_dismissal_candidates(db_url, min_count=2)
    assert len(cands) == 1
    assert cands[0]["sender_email"] == "valid@x.com"


def test_amend_refuses_to_resurrect_a_dismissed_row(db_url):
    """b146: amending a dismissed row must NOT flip it back to 'amended' — that
    would bypass the begin_send / due_for_auto_send dismissed-guards and send a
    reply the user deliberately killed."""
    from app.agent import store

    rid = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, rid) is True
    # the amend must be refused (rowcount 0 → False) and the status stays dismissed
    assert store.mark_amended(db_url, rid, amended_draft="sneaky edit", amended_by="user") is False
    assert store.get(db_url, rid)["status"] == "dismissed"
    # a pending row still amends normally
    rid2 = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-amendable"})
    assert store.mark_amended(db_url, rid2, amended_draft="ok edit", amended_by="user") is True
    assert store.get(db_url, rid2)["status"] == "amended"


def test_normalize_thread_id_permalink_to_hex():
    """Gmail's add-on hands a legacy 'thread-f:<decimal>' permalink id (when a
    draft is open) instead of the API hex id — normalize it so by-thread lookups
    match (live bug: a pushed draft showed 'no draft')."""
    from app.agent.store import normalize_thread_id
    assert normalize_thread_id("thread-f:1868693884786093199") == "19eeef2ffc9eac8f"
    assert normalize_thread_id("thread-a:1868693884786093199") == "19eeef2ffc9eac8f"
    assert normalize_thread_id("19eeef2ffc9eac8f") == "19eeef2ffc9eac8f"   # hex unchanged
    assert normalize_thread_id(None) is None


def test_get_by_thread_matches_permalink_id(db_url):
    """get_by_thread finds a row when queried with the legacy 'thread-f:' permalink
    id (what the add-on sends when a draft is open) — the live "no draft" bug."""
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m1", "thread_id": "19eeef2ffc9eac8f"})
    assert store.get_by_thread(db_url, "thread-f:1868693884786093199")["thread_id"] == "19eeef2ffc9eac8f"
    assert store.get_by_thread(db_url, "19eeef2ffc9eac8f") is not None
