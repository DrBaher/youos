"""b300: thread-STARTING self-authored emails become composition pairs.

The reply-pair extractor only pairs a self-authored message with inbound
messages above it, and `_ingest_thread_documents` skips self-authored mail
entirely — so an email the user composed to open a thread was fetched by the
``in:sent`` nightly window and then dropped everywhere. The voice finetune
learned replying style but never composing style.

These tests are hermetic (local JSON payloads → real SQLite, no network),
mirroring tests/test_ingestion_empty_delta_b169.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.pair_quality import COMPOSITION_PAIR_STRATEGY
from app.ingestion.gmail_threads import ingest_gmail_threads

USER = "me@example.com"

COMPOSED_BODY = (
    "Hi Alice, kicking off the migration project on our side. I've attached the "
    "current plan and would suggest we align on scope before Friday. Best, Me"
)
INBOUND_BODY = (
    "Hi, thanks for the kickoff note — could you walk me through the timeline "
    "assumptions before we commit to the Friday deadline?"
)
REPLY_BODY = (
    "Sure — the timeline assumes staging is ready by Wednesday; happy to walk "
    "you through the details on a quick call tomorrow."
)


def _write_threads(tmp_path: Path, payloads: list[dict]) -> Path:
    export = tmp_path / "threads.json"
    export.write_text(json.dumps({"threads": payloads}), encoding="utf-8")
    return export


def _msg(
    mid: str,
    sender: str,
    body: str,
    *,
    subject: str = "Kickoff plan",
    to: str = "Alice Partner <alice@partner.com>",
    ts: str = "2026-07-01T10:00:00Z",
) -> dict:
    return {
        "id": mid,
        "from_email": sender,
        "from_name": "Me" if sender == USER else "Alice Partner",
        "to": to,
        "body_text": body,
        "subject": subject,
        "timestamp": ts,
        "label_ids": ["SENT"] if sender == USER else [],
    }


def _thread(thread_id: str, messages: list[dict]) -> dict:
    return {"thread_id": thread_id, "account": USER, "messages": messages}


def _ingest(tmp_path: Path, payloads: list[dict], *, db_name: str = "youos.db"):
    db = tmp_path / db_name
    result = ingest_gmail_threads(
        _write_threads(tmp_path, payloads), db_path=db, user_emails=(USER,)
    )
    return db, result


def _pairs(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM reply_pairs ORDER BY id").fetchall()
    finally:
        conn.close()


def _strategy(row: sqlite3.Row) -> str | None:
    return json.loads(row["metadata_json"]).get("pair_strategy")


def test_thread_starting_send_becomes_composition_pair(tmp_path: Path):
    db, result = _ingest(
        tmp_path, [_thread("t1", [_msg("m1", USER, COMPOSED_BODY)])]
    )
    # Pre-b300 this exact shape was b169's "empty delta"; now it lands a row.
    assert result.status == "completed"
    rows = _pairs(db)
    assert len(rows) == 1
    row = rows[0]
    assert _strategy(row) == COMPOSITION_PAIR_STRATEGY
    assert row["inbound_text"].startswith("[compose]")
    assert "To: Alice Partner <alice@partner.com>" in row["inbound_text"]
    assert "Subject: Kickoff plan" in row["inbound_text"]
    assert row["reply_text"] == COMPOSED_BODY
    assert row["document_id"] is None
    # Primary recipient as inbound_author: people the user WRITES TO count as
    # correspondents for the per-sender cap and the needs-reply signal.
    assert "alice@partner.com" in (row["inbound_author"] or "")


def test_only_leading_self_run_counts_as_composition(tmp_path: Path):
    """Opener + immediate self follow-up are compositions; a self message
    after an inbound belongs to the reply-pair extractor."""
    thread = _thread(
        "t2",
        [
            _msg("m1", USER, COMPOSED_BODY, ts="2026-07-01T10:00:00Z"),
            _msg(
                "m2",
                USER,
                "Quick addition — the staging environment link is in the doc as "
                "well, so you can poke around before we talk.",
                ts="2026-07-01T10:05:00Z",
            ),
            _msg("m3", "alice@partner.com", INBOUND_BODY, ts="2026-07-01T11:00:00Z"),
            _msg("m4", USER, REPLY_BODY, ts="2026-07-01T12:00:00Z"),
        ],
    )
    db, _ = _ingest(tmp_path, [thread])
    rows = _pairs(db)
    compositions = [r for r in rows if _strategy(r) == COMPOSITION_PAIR_STRATEGY]
    replies = [r for r in rows if _strategy(r) != COMPOSITION_PAIR_STRATEGY]
    assert len(compositions) == 2
    assert len(replies) == 1
    assert replies[0]["reply_text"] == REPLY_BODY
    assert replies[0]["inbound_text"] == INBOUND_BODY


def test_self_addressed_sends_are_skipped(tmp_path: Path):
    """A send addressed only to the user's own accounts (notes-to-self, and
    YouOS's own Wire/digest machine self-sends) must not become a composition.
    Live verification on prod showed the Wire newsletter dominating the
    ``in:sent`` window — exactly the poison this gate exists for."""
    payloads = [
        _thread("t-self", [_msg("s1", USER, COMPOSED_BODY, to=f"Me <{USER}>")]),
        _thread(
            "t-mixed",
            [_msg("s2", USER, COMPOSED_BODY, to=f"Me <{USER}>, Alice <alice@partner.com>")],
        ),
    ]
    db, _ = _ingest(tmp_path, payloads)
    rows = [r for r in _pairs(db) if _strategy(r) == COMPOSITION_PAIR_STRATEGY]
    # Only the thread with an external recipient survives.
    assert [r["thread_id"] for r in rows] == ["t-mixed"]


def test_junk_openers_are_skipped(tmp_path: Path):
    """Ack-only, forwarded, and inbound-started threads yield no compositions."""
    payloads = [
        _thread("t-ack", [_msg("a1", USER, "Thanks!")]),
        _thread(
            "t-fwd",
            [
                _msg(
                    "f1",
                    USER,
                    "---------- Forwarded message ----------\nFrom someone else",
                    subject="Fwd: something",
                )
            ],
        ),
        _thread("t-inbound", [_msg("i1", "alice@partner.com", INBOUND_BODY)]),
    ]
    db, _ = _ingest(tmp_path, payloads)
    assert all(_strategy(r) != COMPOSITION_PAIR_STRATEGY for r in _pairs(db))


def test_reingest_is_idempotent(tmp_path: Path):
    payloads = [_thread("t1", [_msg("m1", USER, COMPOSED_BODY)])]
    db, _ = _ingest(tmp_path, payloads)
    _ingest(tmp_path, payloads)  # same db file, same payloads
    assert len(_pairs(db)) == 1


# --- consumers that must NOT see composition pairs --------------------------


def _db_with_composition_and_real_pair(tmp_path: Path) -> Path:
    db, _ = _ingest(
        tmp_path,
        [
            _thread("t-comp", [_msg("c1", USER, COMPOSED_BODY)]),
            _thread(
                "t-real",
                [
                    _msg("r1", "alice@partner.com", INBOUND_BODY),
                    _msg("r2", USER, REPLY_BODY, ts="2026-07-01T12:00:00Z"),
                ],
            ),
        ],
    )
    return db


def test_replay_backtest_excludes_compositions(tmp_path: Path):
    from app.evaluation.replay import sample_pairs

    db = _db_with_composition_and_real_pair(tmp_path)
    cases = sample_pairs(f"sqlite:///{db}", n=10, triage_filter=False)
    assert len(cases) == 1
    assert cases[0].inbound_text == INBOUND_BODY


def test_model_compare_sampling_excludes_compositions(tmp_path: Path):
    from app.evaluation.model_compare import sample_reply_pairs

    db = _db_with_composition_and_real_pair(tmp_path)
    cases = sample_reply_pairs(f"sqlite:///{db}", limit=10, min_chars=20)
    assert [c["reference_reply"] for c in cases] == [REPLY_BODY]


def test_retrieval_filter_excludes_compositions():
    from app.retrieval.service import _matches_filters

    assert not _matches_filters(
        source_type="gmail_thread",
        metadata_json=json.dumps({"pair_strategy": COMPOSITION_PAIR_STRATEGY}),
        source_types=(),
        account_emails=(),
    )
    assert _matches_filters(
        source_type="gmail_thread",
        metadata_json=json.dumps(
            {"pair_strategy": "messages_since_last_self_authored_message"}
        ),
        source_types=(),
        account_emails=(),
    )


def test_benchmark_generation_excludes_compositions(tmp_path: Path):
    from scripts.generate_benchmarks import generate_cases

    db = _db_with_composition_and_real_pair(tmp_path)
    cases = generate_cases(db, count=10, sample_size=30, seed=7)
    assert cases, "the real pair should still produce a benchmark case"
    assert all("[compose]" not in c["prompt_text"] for c in cases)


def test_auto_feedback_includes_compositions(tmp_path: Path):
    """The finetune pipeline is the consumer compositions exist FOR: the
    auto-feedback pass must convert them like any organic sent-mail pair."""
    from scripts.extract_auto_feedback import _capture_organic_pairs

    db = _db_with_composition_and_real_pair(tmp_path)
    conn = sqlite3.connect(db)
    try:
        # Minimal missing schema (ingestion's bootstrap already creates
        # feedback_pairs; prod gets draft_events from its own migrations).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS feedback_pairs (id INTEGER PRIMARY KEY,"
            " reply_pair_id INTEGER, inbound_text TEXT, generated_draft TEXT,"
            " edited_reply TEXT, feedback_note TEXT, edit_distance_pct REAL,"
            " rating INTEGER, used_in_finetune INTEGER, organic BOOLEAN DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS draft_events (id INTEGER PRIMARY KEY,"
            " inbound_text TEXT, generated_draft TEXT, created_at TEXT)"
        )
        captured = _capture_organic_pairs(conn)
        assert captured >= 2  # the composition AND the real exchange
        rows = conn.execute(
            "SELECT inbound_text, edited_reply FROM feedback_pairs"
        ).fetchall()
    finally:
        conn.close()
    composed = [r for r in rows if r[0].startswith("[compose]")]
    assert len(composed) == 1
    assert composed[0][1] == COMPOSED_BODY
