"""Triage orchestrator — fetch → filter → draft, with a mocked Google source.

Phase 1 has no persistence; this just pins the end-to-end loop shape so
later phases (UI, scheduling, OAuth) can build on a known-good orchestrator.
"""

from __future__ import annotations

import pytest

from app.agent.inbox_fetch import InboxMessage


@pytest.fixture
def mocked_environment(monkeypatch, tmp_path):
    """A minimal triage env: stubbed inbox fetch, stubbed generate_draft, a
    tmp DB that the SenderHistory can hit without blowing up."""
    # Create an empty SQLite DB with the reply_pairs schema so the history
    # query doesn't fail.
    import sqlite3

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)"
    )
    # β: tests use the real agent_pending_drafts schema so persistence is
    # exercised end-to-end. Call the migration directly rather than copying
    # the DDL here so the test can't drift from prod.
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msgs = [
        InboxMessage(
            message_id="m1",
            thread_id="t1",
            account="you@example.com",
            sender="Alice <alice@partner.com>",
            sender_email="alice@partner.com",
            subject="Pricing question",
            body="Hi — could you confirm the Q3 pricing? Thanks.",
            headers={},
        ),
        InboxMessage(
            message_id="m2",
            thread_id="t2",
            account="you@example.com",
            sender="newsletter@digest.com",
            sender_email="newsletter@digest.com",
            subject="Your weekly digest",
            body="Long body" * 80,
            headers={"list-unsubscribe": "<mailto:unsub@digest.com>"},
        ),
    ]
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: list(msgs))

    class _Resp:
        draft = "Hi Alice, confirmed — Q3 pricing unchanged."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = 0.8  # a good draft — clears the default quality floor

    monkeypatch.setattr(
        "app.generation.service.generate_draft",
        lambda req, **kw: _Resp(),
    )

    return {
        "database_url": f"sqlite:///{db}",
        "configs_dir": tmp_path,
        "messages": msgs,
    }


def test_triage_drafts_for_real_inbound_skips_newsletter(mocked_environment):
    """The pricing question should be drafted; the newsletter should be skipped
    by the list-unsubscribe hard rule."""
    from app.agent.triage import run_triage

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"],
        configs_dir=env["configs_dir"],
    )

    assert result.fetched == 2
    assert result.kept == 1, f"expected 1 draft, got {result.kept}"
    assert len(result.drafts) == 1
    assert len(result.skipped) == 1

    drafted = result.drafts[0]
    assert drafted.message.subject == "Pricing question"
    assert drafted.draft == "Hi Alice, confirmed — Q3 pricing unchanged."
    assert drafted.model_used == "qwen2.5-1.5b-lora"
    assert drafted.error is None

    skipped_msg, skipped_verdict = result.skipped[0]
    assert skipped_msg.subject == "Your weekly digest"
    assert not skipped_verdict.needs_reply


def test_triage_records_draft_errors_without_crashing(mocked_environment, monkeypatch):
    """A generation failure on one message must not kill the whole sweep —
    it's recorded with an ``error`` field, and other messages still draft."""
    from app.agent.triage import run_triage

    def _boom(*a, **k):
        raise RuntimeError("warm server down")

    monkeypatch.setattr("app.generation.service.generate_draft", _boom)

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"],
        configs_dir=env["configs_dir"],
    )

    # The pricing question still tried (and recorded the error); newsletter
    # still skipped. The sweep finished.
    assert result.fetched == 2
    errored = [d for d in result.drafts if d.error]
    assert len(errored) == 1
    assert "warm server down" in errored[0].error
    # kept counts only successful drafts.
    assert result.kept == 0


# --- β: persistence behaviour ---------------------------------------------


def test_triage_persists_drafts_and_is_idempotent_on_repeat_run(mocked_environment):
    """``run_triage`` persists drafts into agent_pending_drafts and is
    idempotent on the Gmail message_id — a second run with the same inbound
    must not create duplicates."""
    from app.agent.store import list_pending
    from app.agent.triage import run_triage

    env = mocked_environment

    r1 = run_triage(account="you@example.com", database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r1.persisted == 1  # the pricing question — newsletter is hard-skipped
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["subject"] == "Pricing question"
    assert rows[0]["tier"] == "draft"
    assert rows[0]["status"] == "pending"
    assert rows[0]["draft"] == "Hi Alice, confirmed — Q3 pricing unchanged."

    # Second run: same message_ids → upsert IGNOREs → no new rows.
    r2 = run_triage(account="you@example.com", database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r2.persisted == 0
    rows2 = list_pending(env["database_url"])
    assert len(rows2) == 1, "repeated triage must not duplicate"


def test_triage_dry_run_does_not_persist(mocked_environment):
    from app.agent.store import list_pending
    from app.agent.triage import run_triage

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        persist=False,
    )
    assert result.persisted == 0
    assert list_pending(env["database_url"]) == []


# --- δ: standing instructions threaded into the prompt + snapshotted -------


def test_standing_instructions_threaded_into_draft_request(mocked_environment, monkeypatch):
    """The triage orchestrator passes ``standing_instructions`` into the
    ``DraftRequest`` so generation can inject it via the same ``extra_constraint``
    hook the cold-outreach nudge uses."""
    seen: dict = {}
    def _spy(req, **kw):
        seen["standing_instructions"] = getattr(req, "standing_instructions", None)
        class _Resp:
            draft = "ok"
            model_used = "stub"
            repairs: list[str] = []
        return _Resp()
    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    env = mocked_environment
    from app.agent.triage import run_triage
    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        standing_instructions="today I'm OOO; politely decline meetings",
        persist=False,
    )
    assert seen["standing_instructions"] == "today I'm OOO; politely decline meetings"


def test_standing_instructions_snapshotted_per_row(mocked_environment):
    """Each persisted row records the standing instructions that were active
    when the draft was generated — auditability after the user changes them."""
    env = mocked_environment
    from app.agent.store import list_pending
    from app.agent.triage import run_triage

    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        standing_instructions="be brief",
    )
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["standing_instructions_snapshot"] == "be brief"


def test_standing_instructions_falls_back_to_config(mocked_environment, monkeypatch):
    """When the caller doesn't pass ``standing_instructions``, the
    orchestrator reads it from ``agent.standing_instructions`` config so the
    background scheduler + manual ``youos triage`` both pick it up."""
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {
            "enabled": True, "interval_minutes": 15, "accounts": [],
            "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
            "standing_instructions": "from config",
        },
    )
    env = mocked_environment
    from app.agent.store import list_pending
    from app.agent.triage import run_triage

    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["standing_instructions_snapshot"] == "from config"


# --- ε: every run writes one agent_audit row -------------------------------


def test_run_triage_writes_an_audit_row_with_counts_and_trigger(mocked_environment):
    from app.agent import store
    from app.agent.triage import run_triage

    env = mocked_environment
    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        trigger="scheduled",
    )
    sweeps = store.list_recent_sweeps(env["database_url"])
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s["account"] == "you@example.com"
    assert s["trigger"] == "scheduled"
    assert s["fetched"] == 2          # one pricing question + one newsletter
    assert s["kept"] == 1
    assert s["persisted"] == 1
    assert s["errors"] == []
    assert s["duration_ms"] is not None and s["duration_ms"] >= 0


def test_run_triage_audit_row_written_even_when_persist_false(mocked_environment):
    """``--dry-run`` (persist=False) doesn't write to agent_pending_drafts,
    but it DOES leave an audit trail of what was swept and why."""
    from app.agent import store
    from app.agent.triage import run_triage

    env = mocked_environment
    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        persist=False, trigger="manual",
    )
    sweeps = store.list_recent_sweeps(env["database_url"])
    assert len(sweeps) == 1
    assert sweeps[0]["persisted"] == 0   # honest record of the dry-run
    assert sweeps[0]["trigger"] == "manual"


def test_run_triage_captures_per_message_errors_in_audit(mocked_environment, monkeypatch):
    """Per-message generation errors land in ``errors_json`` so a transient
    failure shows up in /triage's recent-activity panel."""
    from app.agent import store
    from app.agent.triage import run_triage

    def _boom(req, **kw): raise RuntimeError("warm server down")
    monkeypatch.setattr("app.generation.service.generate_draft", _boom)

    env = mocked_environment
    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    sweeps = store.list_recent_sweeps(env["database_url"])
    assert len(sweeps) == 1
    assert sweeps[0]["kept"] == 0
    assert any("warm server down" in e for e in sweeps[0]["errors"])


# --- ζ: daily cap + strict-local --------------------------------------------


def test_daily_cap_stops_drafting_when_quota_hit(mocked_environment, monkeypatch):
    """When ``agent.daily_draft_cap`` is at or below today's count, the sweep
    stops drafting (recorded as a skip with the cap-reached reason)."""
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {
            "enabled": True, "interval_minutes": 15, "accounts": [],
            "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
            "standing_instructions": "",
            "skip_senders": [], "daily_draft_cap": 1, "strict_local": False,
        },
    )
    env = mocked_environment
    from app.agent.triage import run_triage

    # First run uses the only allowed slot.
    r1 = run_triage(account="you@example.com",
                    database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r1.kept == 1
    assert r1.persisted == 1

    # Second run hits the cap → no drafts persisted, message recorded as
    # capped skip.
    r2 = run_triage(account="you@example.com",
                    database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r2.kept == 0
    assert any(
        any("daily cap reached" in r for r in v.reasons)
        for (_m, v) in r2.skipped
    )


def test_skip_senders_hard_skips_in_triage(mocked_environment, monkeypatch):
    """``agent.skip_senders`` config flows through ``run_triage`` and hits
    the classify hard-skip path."""
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {
            "enabled": True, "interval_minutes": 15, "accounts": [],
            "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
            "standing_instructions": "",
            "skip_senders": ["alice@partner.com"],
            "daily_draft_cap": 0, "strict_local": False,
        },
    )
    env = mocked_environment
    from app.agent.triage import run_triage

    result = run_triage(account="you@example.com",
                        database_url=env["database_url"], configs_dir=env["configs_dir"])
    # Alice was the only draftable inbound; with her on the skip list, kept=0.
    assert result.kept == 0
    assert any(
        any("skip-list" in r for r in v.reasons)
        for (_m, v) in result.skipped
    )


def test_strict_local_passes_through_to_draft_request(mocked_environment, monkeypatch):
    """``agent.strict_local: true`` is reflected on the DraftRequest the
    triage orchestrator builds, so generate_draft enforces no-cloud-fallback
    for that draft."""
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {
            "enabled": True, "interval_minutes": 15, "accounts": [],
            "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
            "standing_instructions": "",
            "skip_senders": [], "daily_draft_cap": 0, "strict_local": True,
        },
    )

    seen: dict = {}
    def _spy(req, **kw):
        seen["strict_local"] = getattr(req, "strict_local", None)
        class _Resp:
            draft = "ok"
            model_used = "stub"
            repairs: list[str] = []
        return _Resp()
    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    env = mocked_environment
    from app.agent.triage import run_triage
    run_triage(account="you@example.com",
               database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert seen["strict_local"] is True


# --- b44: auto-promote skip_senders at sweep tail -----------------------


def test_auto_promote_skips_when_flag_off(mocked_environment, monkeypatch):
    """When agent.auto_promote_skip_senders is False (default), the helper
    is a no-op even if there's noise-dismissal signal in the DB."""
    env = mocked_environment
    db_url = env["database_url"]

    # Seed 3 noise-dismissals from the same sender — would qualify if flag on.
    from app.agent import store
    for i in range(3):
        rid = store.upsert_pending(db_url, **{
            "message_id": f"m-{i}", "thread_id": "t", "account": "you@example.com",
            "sender": "Spammer", "sender_email": "spam@x.com",
            "subject": f"buy now {i}", "body": "blah", "received_at": None,
            "needs_reply_score": 0.6, "reasons": [], "cold_outreach": False,
            "tier": "draft", "draft": "hi", "draft_model": "m",
            "draft_repairs": [], "standing_instructions_snapshot": None,
        })
        store.mark_dismissed(db_url, rid, reason="noise")

    monkeypatch.setattr("app.core.feature_flags.get_flag", lambda key: False if key == "agent.auto_promote_skip_senders" else "")
    from app.agent.triage import _maybe_auto_promote_skip_senders
    added = _maybe_auto_promote_skip_senders(database_url=db_url, account="you@example.com")
    assert added == []


def test_auto_promote_adds_qualifying_senders_when_flag_on(mocked_environment, monkeypatch):
    """When the flag is on, senders with ≥3 noise dismissals get promoted."""
    env = mocked_environment
    db_url = env["database_url"]

    from app.agent import store
    # spam@x.com: 3 dismissals (qualifies); slow@y.com: 2 (doesn't); mixed@z.com: 4 (qualifies).
    for sender, n in [("spam@x.com", 3), ("slow@y.com", 2), ("mixed@z.com", 4)]:
        for i in range(n):
            rid = store.upsert_pending(db_url, **{
                "message_id": f"{sender}-{i}", "thread_id": "t", "account": "you@example.com",
                "sender": sender, "sender_email": sender,
                "subject": f"x{i}", "body": "y", "received_at": None,
                "needs_reply_score": 0.6, "reasons": [], "cold_outreach": False,
                "tier": "draft", "draft": "hi", "draft_model": "m",
                "draft_repairs": [], "standing_instructions_snapshot": None,
            })
            store.mark_dismissed(db_url, rid, reason="noise")

    # Mock the feature-flag read/write so the test doesn't touch the real config.
    state = {"agent.auto_promote_skip_senders": True, "agent.skip_senders": ""}
    monkeypatch.setattr("app.core.feature_flags.get_flag", lambda key: state.get(key, ""))
    saved = {}
    def _set(key, val):
        state[key] = val
        saved[key] = val
        return val
    monkeypatch.setattr("app.core.feature_flags.set_flag", _set)

    from app.agent.triage import _maybe_auto_promote_skip_senders
    added = _maybe_auto_promote_skip_senders(database_url=db_url, account="you@example.com")

    assert set(added) == {"spam@x.com", "mixed@z.com"}
    assert "slow@y.com" not in added
    # The flag value reflects both qualifying senders.
    val = saved["agent.skip_senders"].lower()
    assert "spam@x.com" in val
    assert "mixed@z.com" in val


def test_auto_promote_result_lands_in_audit_row(mocked_environment, monkeypatch):
    """b52: when _maybe_auto_promote_skip_senders returns senders,
    log_sweep captures them on the audit row so /triage Recent activity
    can surface 'the agent auto-skipped 3 senders this sweep'."""
    env = mocked_environment
    db_url = env["database_url"]

    from app.agent import store
    for i in range(3):
        rid = store.upsert_pending(db_url, **{
            "message_id": f"m-prom-{i}", "thread_id": "t", "account": "you@example.com",
            "sender": "Spammer", "sender_email": "spam@noise.com",
            "subject": f"x{i}", "body": "y", "received_at": None,
            "needs_reply_score": 0.6, "reasons": [], "cold_outreach": False,
            "tier": "draft", "draft": "hi", "draft_model": "m",
            "draft_repairs": [], "standing_instructions_snapshot": None,
        })
        store.mark_dismissed(db_url, rid, reason="noise")

    state = {"agent.auto_promote_skip_senders": True, "agent.skip_senders": ""}
    monkeypatch.setattr("app.core.feature_flags.get_flag", lambda key: state.get(key, ""))
    monkeypatch.setattr("app.core.feature_flags.set_flag", lambda k, v: (state.update({k: v}) or v))

    from app.agent.triage import run_triage
    run_triage(account="you@example.com",
               database_url=db_url, configs_dir=env["configs_dir"])

    sweeps = store.list_recent_sweeps(db_url, account="you@example.com")
    assert sweeps[0]["auto_promoted"] == ["spam@noise.com"]


def test_auto_promote_skips_senders_already_on_skip_list(mocked_environment, monkeypatch):
    """If a candidate is already in agent.skip_senders, the helper doesn't
    re-add — and so doesn't write the flag at all if everyone's already there."""
    env = mocked_environment
    db_url = env["database_url"]

    from app.agent import store
    for i in range(3):
        rid = store.upsert_pending(db_url, **{
            "message_id": f"m-{i}", "thread_id": "t", "account": "you@example.com",
            "sender": "Bot", "sender_email": "bot@noise.com",
            "subject": f"x{i}", "body": "y", "received_at": None,
            "needs_reply_score": 0.6, "reasons": [], "cold_outreach": False,
            "tier": "draft", "draft": "hi", "draft_model": "m",
            "draft_repairs": [], "standing_instructions_snapshot": None,
        })
        store.mark_dismissed(db_url, rid, reason="noise")

    state = {"agent.auto_promote_skip_senders": True, "agent.skip_senders": "bot@noise.com"}
    monkeypatch.setattr("app.core.feature_flags.get_flag", lambda key: state.get(key, ""))
    set_calls = []
    def _set(key, val):
        set_calls.append((key, val))
        state[key] = val
        return val
    monkeypatch.setattr("app.core.feature_flags.set_flag", _set)

    from app.agent.triage import _maybe_auto_promote_skip_senders
    added = _maybe_auto_promote_skip_senders(database_url=db_url, account="you@example.com")
    assert added == []
    # No flag write when nothing new to add.
    assert set_calls == []


# --- Tier-0 hardening: failed-sweep visibility (#4) + concurrency guard (#6) ---


def test_failed_sweep_logs_audit_row_and_reraises(mocked_environment, monkeypatch):
    """A sweep that raises during fetch (the #1 unattended failure: expired gog
    auth) must STILL write an agent_audit row with the error and re-raise — so
    the failure is visible and the observability success-rate reflects it,
    instead of staying green while the agent is dead."""
    import pytest

    from app.agent import store
    from app.agent.triage import run_triage

    env = mocked_environment

    def _boom(*a, **k):
        raise RuntimeError("gog auth expired")

    monkeypatch.setattr("app.agent.triage.fetch_unread", _boom)

    with pytest.raises(RuntimeError, match="gog auth expired"):
        run_triage(
            account="you@example.com",
            database_url=env["database_url"],
            configs_dir=env["configs_dir"],
        )

    sweeps = store.list_recent_sweeps(env["database_url"], account="you@example.com", limit=5)
    assert len(sweeps) == 1, "a failed sweep must still log one audit row"
    assert any("gog auth expired" in e for e in sweeps[0]["errors"])

    agg = store.sweep_aggregate(env["database_url"], account="you@example.com")
    assert agg["sweeps"] == 1
    assert agg["successful"] == 0
    assert agg["success_rate"] == 0.0


def test_concurrent_sweep_is_skipped_when_account_locked(mocked_environment, monkeypatch):
    """If a sweep for the account is already in progress, a second run is a
    no-op (it must not fetch or draft) — so two overlapping sweeps can't each
    consume the daily cap budget."""
    from app.agent import triage

    env = mocked_environment
    calls = {"fetch": 0}

    def _count_fetch(*a, **k):
        calls["fetch"] += 1
        return []

    monkeypatch.setattr("app.agent.triage.fetch_unread", _count_fetch)

    lock = triage._account_lock("you@example.com")
    assert lock.acquire()
    try:
        result = triage.run_triage(
            account="you@example.com",
            database_url=env["database_url"],
            configs_dir=env["configs_dir"],
        )
    finally:
        lock.release()

    assert result.fetched == 0
    assert result.persisted == 0
    assert calls["fetch"] == 0, "the locked-out sweep must not run the body"


# --- Borderline LLM adjudication (Phase A2) --------------------------------


def test_adjudication_vetoes_broadcast_borderline_draft(mocked_environment, monkeypatch):
    """A would-be draft the heuristic accepts is demoted when the model calls
    it a broadcast. The veto only ever demotes — the message surfaces for
    review instead of being drafted."""
    from app.agent import triage
    from app.core import model_server

    env = mocked_environment
    monkeypatch.setattr(triage, "_adjudication_config", lambda: {"enabled": True, "high": 0.95})
    monkeypatch.setattr(model_server, "is_enabled", lambda: True)
    monkeypatch.setattr(model_server, "complete", lambda *a, **k: "BROADCAST")

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    # Alice (the only non-newsletter) is vetoed → nothing drafted.
    assert result.kept == 0, f"expected the broadcast veto to suppress the draft, got {result.drafts}"


def test_adjudication_keeps_personal_borderline_draft(mocked_environment, monkeypatch):
    """A PERSONAL verdict leaves the heuristic decision intact — the draft stands."""
    from app.agent import triage
    from app.core import model_server

    env = mocked_environment
    monkeypatch.setattr(triage, "_adjudication_config", lambda: {"enabled": True, "high": 0.95})
    monkeypatch.setattr(model_server, "is_enabled", lambda: True)
    monkeypatch.setattr(model_server, "complete", lambda *a, **k: "PERSONAL")

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.kept == 1


def test_adjudication_noop_when_disabled(mocked_environment, monkeypatch):
    """Flag off → the model is never consulted and the draft stands."""
    from app.agent import triage
    from app.core import model_server

    env = mocked_environment
    monkeypatch.setattr(triage, "_adjudication_config", lambda: {"enabled": False, "high": 0.95})
    monkeypatch.setattr(
        model_server, "complete",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called when disabled")),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.kept == 1


# --- Fact grounding in the sweep (Phase A3) --------------------------------


def test_fact_extraction_runs_for_drafted_mail_when_enabled(mocked_environment, monkeypatch):
    from app.agent import triage
    from app.core import facts_extractor

    env = mocked_environment
    monkeypatch.setattr(triage, "_extract_facts_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        facts_extractor, "extract_and_save",
        lambda note, db_path, **kw: calls.append((note, kw.get("sender_email"))) or [],
    )

    triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    # Exactly the drafted (Alice) message had its body harvested.
    assert len(calls) == 1
    assert "Q3 pricing" in calls[0][0]
    assert calls[0][1] == "alice@partner.com"


def test_fact_extraction_skipped_when_disabled(mocked_environment, monkeypatch):
    from app.agent import triage
    from app.core import facts_extractor

    env = mocked_environment
    monkeypatch.setattr(triage, "_extract_facts_enabled", lambda: False)
    monkeypatch.setattr(
        facts_extractor, "extract_and_save",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not extract when disabled")),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.kept == 1


# --- Tiered auto-push (audit Tier 2) ---------------------------------------


def _autopush_cfg(**over):
    base = {
        "enabled": True, "dry_run": True, "confidence_floor": 0.85,
        "quality_floor": 0.5,
        "min_pairs": 0, "daily_push_cap": 5, "whitelist": ["@partner.com"],
    }
    base.update(over)
    return base


def test_auto_push_dry_run_logs_would_push_without_writing(mocked_environment, monkeypatch):
    from app.agent import store, triage

    env = mocked_environment
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=True))

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    actions = {(a["id"], a["action"]) for a in result.auto_pushed}
    assert any(a == "would_push" for _, a in actions), result.auto_pushed
    # Dry-run must NOT have created any Gmail draft.
    sent = store.list_pending(env["database_url"], status="sent")
    assert sent == []


def test_auto_push_live_creates_gmail_draft_for_whitelisted_high_confidence(mocked_environment, monkeypatch):
    from app.agent import store, triage
    from app.ingestion import gmail_write

    env = mocked_environment
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=False))
    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **kw: gmail_write.GmailDraftResult(draft_id="gd_auto", raw_response={"id": "gd_auto"}),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    pushed = [a for a in result.auto_pushed if a["action"] == "pushed"]
    assert len(pushed) == 1, result.auto_pushed
    assert pushed[0]["gmail_draft_id"] == "gd_auto"

    sent = store.list_pending(env["database_url"], status="sent")
    assert len(sent) == 1
    assert sent[0]["gmail_draft_id"] == "gd_auto"


def test_auto_push_empty_whitelist_pushes_nothing(mocked_environment, monkeypatch):
    from app.agent import store, triage

    env = mocked_environment
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=False, whitelist=[]))
    monkeypatch.setattr(
        "app.ingestion.gmail_write.create_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not push with empty whitelist")),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.auto_pushed == []
    assert store.list_pending(env["database_url"], status="sent") == []


def test_auto_push_below_floor_is_not_pushed(mocked_environment, monkeypatch):
    from app.agent import triage

    env = mocked_environment
    # Floor above the Alice row's ~0.90 score → nothing qualifies.
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=True, confidence_floor=0.99))
    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.auto_pushed == []


def test_auto_push_below_quality_floor_is_not_pushed(mocked_environment, monkeypatch):
    """A high needs-reply score with a WEAK draft must be held, not pushed —
    the gate is on the draft's quality, not just whether it deserves a reply."""
    from app.agent import store, triage

    env = mocked_environment
    # The Alice row clears the confidence floor (~0.90) but the draft scores low.
    class _WeakResp:
        draft = "Hi Alice, confirmed — Q3 pricing unchanged."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = 0.30

    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _WeakResp())
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=False, quality_floor=0.5))
    monkeypatch.setattr(
        "app.ingestion.gmail_write.create_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not push a low-quality draft")),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.auto_pushed == []
    assert store.list_pending(env["database_url"], status="sent") == []


def test_auto_push_unscored_draft_is_held(mocked_environment, monkeypatch):
    """A draft whose quality scoring failed (quality_score is None) is treated
    as below the floor — auto-push is conservative when it can't judge itself."""
    from app.agent import store, triage

    env = mocked_environment
    class _UnscoredResp:
        draft = "Hi Alice, confirmed — Q3 pricing unchanged."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = None

    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _UnscoredResp())
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=False))
    monkeypatch.setattr(
        "app.ingestion.gmail_write.create_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not push an unscored draft")),
    )

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert result.auto_pushed == []
    assert store.list_pending(env["database_url"], status="sent") == []


def test_auto_push_holds_high_stakes_mail(monkeypatch, tmp_path):
    """A whitelisted, high-confidence, high-quality draft is still held when the
    inbound is high-stakes (money/legal) — escalation never auto-acts on these."""
    import sqlite3

    from app.agent import store, triage
    from app.agent.inbox_fetch import InboxMessage
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msg = InboxMessage(
        message_id="hs1", thread_id="t1", account="you@example.com",
        sender="Alice <alice@partner.com>", sender_email="alice@partner.com",
        subject="Contract for countersignature",
        body="Please review and sign the attached agreement — payment is due on signing.",
        headers={},
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [msg])

    class _Resp:
        draft = "Hi Alice, happy to review and get back to you."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = 0.9

    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _Resp())
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=True))
    monkeypatch.setattr(
        "app.ingestion.gmail_write.create_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("high-stakes mail must not auto-push")),
    )

    result = triage.run_triage(
        account="you@example.com", database_url=f"sqlite:///{db}", configs_dir=tmp_path,
    )
    # Drafted (so the human can review) but NOT auto-pushed.
    assert result.kept == 1
    assert result.auto_pushed == []
    assert store.list_pending(f"sqlite:///{db}", status="sent") == []


def test_thread_history_threaded_into_draft_request(monkeypatch, tmp_path):
    """The agent passes the inbound's thread_history to generate_draft so the
    drafter has conversation context."""
    import sqlite3

    from app.agent.inbox_fetch import InboxMessage

    db = tmp_path / "th.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msg = InboxMessage(
        message_id="m1", thread_id="t1", account="you@example.com",
        sender="Alice <alice@x.com>", sender_email="alice@x.com",
        subject="Re: Q3", body="Any update on pricing?", headers={},
        thread_history=[{"sender": "Alice <alice@x.com>", "text": "Here's the deck."}],
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [msg])

    seen = {}

    class _Resp:
        draft = "Hi Alice, pricing is unchanged."
        model_used = "m"
        repairs: list[str] = []

    def _spy(req, **kw):
        seen["thread_history"] = req.thread_history
        return _Resp()

    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    from app.agent.triage import run_triage
    run_triage(
        account="you@example.com",
        database_url=f"sqlite:///{db}", configs_dir=tmp_path,
    )
    assert seen["thread_history"] == [{"sender": "Alice <alice@x.com>", "text": "Here's the deck."}]


def test_rules_prepend_reaches_generate_draft(mocked_environment, monkeypatch):
    """A prepend rule for the sender injects its instruction into the draft's
    standing_instructions."""
    from app.agent import triage

    env = mocked_environment
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [
        {"match": {"domain": "@partner.com"}, "action": "prepend",
         "value": "Confirm the timeline and CC Jane."},
    ])
    monkeypatch.setattr("app.agent.rules.rules_need_intent", lambda rules: False)

    seen = {}

    class _Resp:
        draft = "ok"
        model_used = "m"
        repairs: list[str] = []

    def _spy(req, **kw):
        seen["si"] = req.standing_instructions
        return _Resp()

    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    assert "Confirm the timeline and CC Jane." in (seen["si"] or "")


def test_rules_skip_drops_message_from_drafting(mocked_environment, monkeypatch):
    from app.agent import store, triage

    env = mocked_environment
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [
        {"match": {"domain": "@partner.com"}, "action": "skip", "value": None},
    ])
    monkeypatch.setattr("app.agent.rules.rules_need_intent", lambda rules: False)

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    # The Alice (@partner.com) message must not be drafted.
    drafts = store.list_pending(env["database_url"], status="pending", tier="draft")
    assert all("partner.com" not in (r.get("sender_email") or "") for r in drafts)
    assert result.persisted == 0 or all(
        "partner.com" not in (r.get("sender_email") or "") for r in drafts
    )


def test_archive_rule_excludes_message_from_drafting(mocked_environment, monkeypatch):
    """A message routed to archive by a rule must NOT also be drafted/persisted —
    the user said get it out of the inbox."""
    from app.agent import actions as act
    from app.agent import triage

    env = mocked_environment
    # Route the Alice (@partner.com) message to archive (dry-run is fine: the
    # rule MATCHED, so it's dropped from drafting either way).
    monkeypatch.setattr(act, "_actions_config", lambda: {"enabled": True, "dry_run": True, "daily_cap": 50})
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [
        {"match": {"domain": "@partner.com"}, "action": "archive", "value": None},
    ])
    monkeypatch.setattr("app.agent.rules.rules_need_intent", lambda rules: False)

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    # Alice was archived → not drafted; the newsletter is hard-skipped anyway.
    assert result.kept == 0
    assert any(a["action"]["type"] == "archive" for a in result.mailbox_actions)


def test_hold_rule_drafts_but_blocks_auto_push(mocked_environment, monkeypatch):
    """A 'hold' rule (content predicate) still drafts the reply, but the row is
    excluded from auto-push — so it can never be auto-sent either."""
    from app.agent import store, triage

    env = mocked_environment
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [
        {"match": {"body_contains": "pricing"}, "action": "hold", "value": None},
    ])
    monkeypatch.setattr("app.agent.rules.rules_need_intent", lambda rules: False)
    # Auto-push otherwise WOULD push this whitelisted, high-quality draft.
    monkeypatch.setattr(triage, "_auto_push_config", lambda: _autopush_cfg(dry_run=True))

    result = triage.run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    # Drafted (the reply is ready for the human)...
    assert result.kept == 1
    drafts = store.list_pending(env["database_url"], status="pending", tier="draft")
    assert any("partner.com" in (r.get("sender_email") or "") for r in drafts)
    # ...but held back from auto-push (and therefore auto-send).
    assert result.auto_pushed == []


def test_calendar_proposes_slots_for_meeting_requests(monkeypatch, tmp_path):
    """When calendar is enabled and the inbound is a meeting request, the
    agent injects real open slots into the draft instructions."""
    import sqlite3

    from app.agent import triage
    from app.agent.inbox_fetch import InboxMessage

    db = tmp_path / "cal.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msg = InboxMessage(
        message_id="m1", thread_id="t1", account="you@example.com",
        sender="Bob <bob@x.com>", sender_email="bob@x.com",
        subject="Sync", body="Can we schedule a call to sync next week?", headers={},
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [msg])
    monkeypatch.setattr("app.agent.triage._calendar_config", lambda: {
        "enabled": True, "tz": "Europe/Vienna", "business_days": 5,
        "work_start_hour": 9, "work_end_hour": 17, "slot_minutes": 30, "max_slots": 3,
    })
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [])
    from datetime import datetime, timezone
    monkeypatch.setattr(
        "app.agent.calendar.propose_open_slot_intervals",
        lambda account, **kw: [
            (datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)),
            (datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc), datetime(2026, 6, 3, 10, 30, tzinfo=timezone.utc)),
        ],
    )

    seen = {}

    class _Resp:
        draft = "ok"
        model_used = "m"
        repairs: list[str] = []

    def _spy(req, **kw):
        seen["si"] = req.standing_instructions
        return _Resp()

    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    triage.run_triage(account="you@example.com", database_url=f"sqlite:///{db}", configs_dir=tmp_path)
    assert "You are free at:" in (seen["si"] or "")
    assert "Tue Jun 2" in (seen["si"] or "")


def test_thread_summary_persisted_for_long_threads(monkeypatch, tmp_path):
    """When summarize_threads is on and the inbound is on a long thread, the
    catch-up summary is generated and persisted on the row."""
    import sqlite3

    from app.agent import store, triage
    from app.agent.inbox_fetch import InboxMessage

    db = tmp_path / "ts.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msg = InboxMessage(
        message_id="m1", thread_id="t1", account="you@example.com",
        sender="Alice <alice@x.com>", sender_email="alice@x.com",
        subject="Q3", body="So where did we land?", headers={},
        thread_history=[{"sender": f"P{i}", "text": f"point {i}"} for i in range(5)],
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [msg])
    monkeypatch.setattr("app.core.config.load_config",
                        lambda *a, **k: {"agent": {"summarize_threads": {"enabled": True, "min_messages": 3}}})
    monkeypatch.setattr("app.agent.thread_summary.summarize_thread",
                        lambda hist, **kw: "Pricing agreed; start date open.")

    class _Resp:
        draft = "ok"
        model_used = "m"
        repairs: list[str] = []
    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _Resp())

    triage.run_triage(account="you@example.com", database_url=f"sqlite:///{db}", configs_dir=tmp_path)
    rows = store.list_pending(f"sqlite:///{db}", status="pending", tier="draft")
    assert len(rows) == 1
    assert rows[0]["thread_summary"] == "Pricing agreed; start date open."


def test_sweep_lock_is_cross_process(monkeypatch, tmp_path):
    """b135: the per-account sweep lock must serialize ACROSS processes, not just
    threads — else the `youos triage` CLI racing the daemon each reads the same
    daily-cap count and overshoots it ~2x. Simulate a second process by holding
    the flock on an independent fd."""
    import fcntl
    import os

    from app.agent import triage

    monkeypatch.setattr(triage, "_sweep_lockfile", lambda acct: tmp_path / f".sweep-{acct}.lock")
    foreign = os.open(tmp_path / ".sweep-acct.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)  # "process B" holds it
    try:
        assert triage._account_lock("acct").acquire() is False  # blocked across processes
    finally:
        fcntl.flock(foreign, fcntl.LOCK_UN)
        os.close(foreign)
    # once the other process releases, this process can acquire.
    lk = triage._account_lock("acct")
    assert lk.acquire() is True
    lk.release()


def test_sender_whitelist_catch_all_star():
    """A bare '*' (or '@*') whitelist entry matches every sender — the
    'auto-push everything' config. Exact/@domain entries still work; empty
    whitelist still matches nothing."""
    from app.agent.triage import _sender_in_whitelist as w
    assert w("anyone@anywhere.com", ["*"]) is True
    assert w("anyone@anywhere.com", ["@*"]) is True
    assert w("a@x.com", ["a@x.com"]) is True
    assert w("b@x.com", ["a@x.com"]) is False
    assert w("a@x.com", ["@x.com"]) is True
    assert w("a@x.com", []) is False
