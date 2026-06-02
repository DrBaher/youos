"""b189 — surface TIME-CRITICAL email first.

Urgency was detected (the "urgent" intent label, assess_stakes) but discarded:
the pending queue sorted on needs_reply_score alone and the digest's "worth
attention" line was a subject-keyword guess. b189 adds compute_urgency_score and
wires it into (a) list_pending ordering and (b) the digest highlight, plus an
additive urgency_score column.

Covers:
  (i)   score composition — urgent/deadline/high-stakes score high, routine low,
        multilingual DE/FR/ES deadline markers bump it, score clamped [0, 1].
  (ii)  list_pending ordering — a high-urgency low-needs_reply item sorts ABOVE
        a non-urgent high-needs_reply item; needs_reply is the tiebreaker.
  (iii) digest "worth attention" picks the urgent item (data-driven), with a
        deterministic fail-safe fallback when the model is off.
  (iv)  the additive migration adds the column and defaults existing rows to 0.0.
  (v)   SAFETY: urgency does NOT lower any send/auto-push threshold; the
        never-send seam stays unreachable.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.urgency import compute_urgency_score

# ---------------------------------------------------------------------------
# (i) score composition
# ---------------------------------------------------------------------------


def test_urgent_deadline_highstakes_scores_high():
    score, reasons = compute_urgency_score(
        subject="URGENT: contract sign-off needed by EOD",
        body="Please approve the wire transfer today. Can you confirm?",
    )
    assert score >= 0.8
    # transparency: reasons explain the score
    assert any("urgency marker" in r for r in reasons)
    assert any("deadline" in r for r in reasons)
    assert any("high-stakes" in r for r in reasons)


def test_routine_message_scores_low():
    score, reasons = compute_urgency_score(
        subject="lunch sometime?",
        body="No rush at all — was thinking we could grab lunch next month.",
    )
    assert score <= 0.2
    # "next month" carries no urgency marker; the only signal is the ending '?'
    assert score < 0.5


def test_urgent_intent_label_contributes():
    # The 'urgent' intent label fires off the body keywords; passing it in
    # should lift the score and add the intent reason.
    score, reasons = compute_urgency_score(
        subject="quick one",
        body="this is critical and time-sensitive",
        intents=["urgent"],
    )
    assert score >= 0.45
    assert any("urgent' intent" in r for r in reasons)


@pytest.mark.parametrize(
    "subject,body",
    [
        ("Dringend: Frist heute", "Bitte bis heute antworten, es eilt."),   # DE
        ("Urgent: delai demain", "Reponse avant aujourd hui svp."),          # FR
        ("Urgente: plazo hoy", "Necesito respuesta antes de manana."),       # ES
    ],
)
def test_multilingual_deadline_markers_bump_score(subject, body):
    score, reasons = compute_urgency_score(subject=subject, body=body)
    # A routine non-urgent message scores ~0; these all carry urgency+deadline.
    assert score >= 0.5, (subject, score, reasons)
    assert any("deadline" in r for r in reasons)


def test_score_is_clamped_to_unit_interval():
    # Pile on every signal — must never exceed 1.0.
    score, _ = compute_urgency_score(
        subject="URGENT deadline today, due by Friday, asap",
        body="Critical: approve the $5000 payment immediately. Can you confirm?",
        intents=["urgent"],
        stakes="high",
    )
    assert 0.0 <= score <= 1.0
    assert score == 1.0

    low, _ = compute_urgency_score(subject="", body="")
    assert low == 0.0


# ---------------------------------------------------------------------------
# (ii) list_pending ordering
# ---------------------------------------------------------------------------


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
    subject="Subject",
    body="Body",
    received_at="2026-05-28T10:00:00Z",
    needs_reply_score=0.5,
    reasons=[],
    cold_outreach=False,
    tier="draft",
    draft="draft text",
    draft_model="local",
    draft_repairs=[],
    standing_instructions_snapshot=None,
)


def test_high_urgency_low_needs_reply_sorts_above_low_urgency_high_needs_reply(db_url):
    from app.agent import store

    # Urgent but lower needs-reply confidence.
    store.upsert_pending(db_url, **{
        **_DEFAULTS, "message_id": "urgent", "needs_reply_score": 0.55,
        "urgency_score": 0.9, "urgency_reasons": ["deadline / time-sensitivity marker"],
    })
    # Routine but high needs-reply confidence.
    store.upsert_pending(db_url, **{
        **_DEFAULTS, "message_id": "routine", "needs_reply_score": 0.95,
        "urgency_score": 0.0,
    })

    rows = store.list_pending(db_url)
    assert [r["message_id"] for r in rows] == ["urgent", "routine"]
    # urgency_reasons rehydrated for the UI.
    top = rows[0]
    assert top["urgency_score"] == pytest.approx(0.9)
    assert top["urgency_reasons"] == ["deadline / time-sensitivity marker"]


def test_needs_reply_is_tiebreaker_when_urgency_equal(db_url):
    from app.agent import store

    store.upsert_pending(db_url, **{
        **_DEFAULTS, "message_id": "lo", "needs_reply_score": 0.60, "urgency_score": 0.5,
    })
    store.upsert_pending(db_url, **{
        **_DEFAULTS, "message_id": "hi", "needs_reply_score": 0.90, "urgency_score": 0.5,
    })
    rows = store.list_pending(db_url)
    assert [r["message_id"] for r in rows] == ["hi", "lo"]


# ---------------------------------------------------------------------------
# (iii) digest "worth attention" is data-driven
# ---------------------------------------------------------------------------


def test_digest_worth_attention_picks_urgent_item_modelless():
    from app.agent.digest_tasks import build_digest_body

    items = [
        {"id": "1", "from": "Bob <bob@x.com>", "subject": "weekly newsletter", "date": "Mon"},
        {"id": "2", "from": "Carol <carol@y.com>", "subject": "URGENT: deadline today", "date": "Mon"},
    ]
    # An empty model return forces the deterministic fallback path (the model
    # being available in some envs would otherwise paraphrase the line).
    body = build_digest_body(items, model="local", complete_fn=lambda _p: "")
    assert "Worth attention:" in body
    # The urgent item is named; the newsletter is not.
    wa_line = next(ln for ln in body.splitlines() if ln.startswith("Worth attention:"))
    assert "URGENT: deadline today" in wa_line
    assert "weekly newsletter" not in wa_line


def test_digest_worth_attention_nothing_urgent_when_all_routine():
    from app.agent.digest_tasks import build_digest_body

    items = [
        {"id": "1", "from": "Bob <bob@x.com>", "subject": "weekly newsletter", "date": "Mon"},
        {"id": "2", "from": "Dan <dan@z.com>", "subject": "fyi notes from sync", "date": "Tue"},
    ]
    body = build_digest_body(items, model="local", complete_fn=lambda _p: "")
    assert "Worth attention: nothing urgent" in body


def test_digest_marks_urgent_inline_for_the_model():
    from app.agent.digest_tasks import build_digest_body

    captured = {}

    def fake_model(prompt: str) -> str:
        captured["prompt"] = prompt
        return "summary line"

    items = [
        {"id": "1", "from": "Bob", "subject": "newsletter", "date": "Mon"},
        {"id": "2", "from": "Carol", "subject": "Urgent: respond by EOD", "date": "Mon"},
    ]
    body = build_digest_body(items, complete_fn=fake_model)
    # The urgent item is marked [URGENT] in the source list the model sees.
    assert "[URGENT]" in captured["prompt"]
    # And the deterministic worth-attention line is appended regardless.
    assert "Worth attention:" in body
    assert "Urgent: respond by EOD" in body


# ---------------------------------------------------------------------------
# (iv) additive migration
# ---------------------------------------------------------------------------


def test_migration_adds_urgency_column_and_defaults_existing_rows(tmp_path):
    """An OLD-schema DB (no urgency columns) self-heals: the migration adds the
    column AND existing rows default to urgency_score=0.0."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # Minimal OLD agent_pending_drafts WITHOUT the b189 columns.
    conn.execute(
        """
        CREATE TABLE agent_pending_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            account TEXT NOT NULL,
            sender TEXT, sender_email TEXT, subject TEXT, body TEXT, received_at TEXT,
            needs_reply_score REAL NOT NULL,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            cold_outreach INTEGER NOT NULL DEFAULT 0,
            tier TEXT NOT NULL,
            draft TEXT, draft_model TEXT,
            draft_repairs_json TEXT NOT NULL DEFAULT '[]',
            standing_instructions_snapshot TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            amended_draft TEXT, sent_at TEXT, dismissed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, "
        "needs_reply_score, tier) VALUES ('old-1', 't', 'a@x.com', 0.7, 'draft')"
    )
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    assert "urgency_score" not in cols_before

    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    conn.commit()

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    assert "urgency_score" in cols_after
    assert "urgency_reasons_json" in cols_after
    # Existing row defaulted to 0.0 (not NULL) and an empty reasons list.
    row = conn.execute(
        "SELECT urgency_score, urgency_reasons_json FROM agent_pending_drafts WHERE message_id='old-1'"
    ).fetchone()
    conn.close()
    assert row[0] == 0.0
    assert row[1] == "[]"


# ---------------------------------------------------------------------------
# (v) SAFETY — urgency never lowers a send/auto-push gate; never-send intact
# ---------------------------------------------------------------------------


def test_urgency_does_not_lower_auto_push_or_send_gates():
    """decide_action is the auto-push/auto-send gate. It takes NO urgency input;
    feeding a maximally-urgent message through it must NOT change the verdict vs
    the identical non-urgent message. Urgency is ordering/visibility only."""
    from app.agent.escalation import decide_action

    # A borderline-quality, borderline-confidence draft that the gate QUEUEs
    # (does not auto-act). Urgency must not flip it to auto_act. Use urgency
    # markers that are NOT also high-stakes words ("asap"/"today"/"tomorrow"),
    # so the comparison isolates urgency from the (separate) stakes veto.
    urgent_decision = decide_action(
        quality_score=0.75, needs_reply_score=0.70,
        subject="respond asap, need it today",
        body="time-sensitive, please reply immediately today or tomorrow?",
    )
    routine_decision = decide_action(
        quality_score=0.75, needs_reply_score=0.70,
        subject="quick note", body="whenever you get a chance",
    )
    # Same gate verdict regardless of urgency — urgency is not an input.
    assert urgent_decision.action == routine_decision.action
    assert urgent_decision.action == "queue"
    assert urgent_decision.action != "auto_act"


def test_urgency_signature_absent_from_send_gate():
    """Belt-and-braces: the escalation gate's public signature must not grow an
    urgency parameter (which would invite wiring urgency into an outbound
    decision). Locks the safety contract at the seam."""
    import inspect

    from app.agent.escalation import assess_stakes, decide_action

    assert "urgency" not in inspect.signature(decide_action).parameters
    assert "urgency" not in inspect.signature(assess_stakes).parameters


def test_high_urgency_high_stakes_still_blocks_auto_act():
    """High-stakes content is a hard veto on auto_act. An urgent + high-stakes
    message (exactly the case urgency might tempt one to fast-track) must STILL
    be held for a human — urgency makes it no less conservative."""
    from app.agent.escalation import decide_action

    decision = decide_action(
        quality_score=0.99, needs_reply_score=0.99,
        subject="URGENT: wire the deposit today",
        body="Please transfer the payment immediately — deadline is EOD.",
    )
    assert decision.action == "ask"
    assert decision.stakes == "high"
