"""Inbox-replay backtest (b234): holdout exclusion + replay scoring.

The backtest's validity rests on one property: a replayed inbound must not be
able to retrieve its own stored answer. Without the exclusion, the identical
inbound ranks its own pair top-1 and the eval scores the corpus, not the model.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.evaluation.replay import (
    ReplayCase,
    aggregate,
    evaluate_case,
    run_replay,
    sample_pairs,
)

# --- retrieval holdout -------------------------------------------------------


@pytest.fixture
def corpus_db(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir()
    (tmp_path / "configs").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parents[1]
    (docs / "schema.sql").write_text((repo / "docs" / "schema.sql").read_text())
    (tmp_path / "configs" / "retrieval.yaml").write_text(
        (repo / "configs" / "retrieval.yaml").read_text()
    )
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")
    from app.core.config import load_config

    load_config.cache_clear()
    from app.core.settings import get_settings

    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database

    bootstrap_database()

    url = f"sqlite:///{tmp_path}/var/youos.db"
    conn = sqlite3.connect(tmp_path / "var" / "youos.db")
    _ins = (
        "INSERT INTO reply_pairs (id, source_type, source_id, thread_id, inbound_text,"
        " reply_text, inbound_author, reply_author, paired_at) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(_ins, (
        1, "gmail_thread", "s1", "th-1",
        "Could you confirm the quarterly pricing for the Vienna lab rollout?",
        "Confirmed — quarterly pricing for Vienna stays unchanged.",
        "Alice <alice@x.com>", "Me <me@x.com>", "2026-06-01",
    ))
    conn.execute(_ins, (
        2, "gmail_thread", "s2", "th-2",
        "Different topic: the Berlin onboarding schedule needs review.",
        "Berlin onboarding moves to Monday.",
        "Bob <bob@y.com>", "Me <me@x.com>", "2026-06-02",
    ))
    conn.commit()
    conn.close()
    return url


def test_retrieval_excludes_heldout_pair_and_thread(corpus_db, tmp_path):
    from app.retrieval.service import RetrievalRequest, RetrievalService

    svc = RetrievalService.from_database_url(database_url=corpus_db, configs_dir=tmp_path / "configs")
    q = "Could you confirm the quarterly pricing for the Vienna lab rollout?"

    baseline = svc.retrieve(RetrievalRequest(query=q))
    assert any(m.reply_pair_id == 1 for m in baseline.reply_pairs), "sanity: own pair retrieved without exclusion"

    held_out = svc.retrieve(
        RetrievalRequest(query=q, exclude_reply_pair_ids=(1,), exclude_thread_ids=("th-1",))
    )
    assert not any(m.reply_pair_id == 1 for m in held_out.reply_pairs)
    assert not any(m.thread_id == "th-1" for m in held_out.reply_pairs)
    assert not any(m.thread_id == "th-1" for m in held_out.documents)
    assert not any(m.thread_id == "th-1" for m in held_out.chunks)


def test_sample_pairs_filters_automation_and_trivial(corpus_db):
    conn = sqlite3.connect(corpus_db.removeprefix("sqlite:///"))
    _ins = (
        "INSERT INTO reply_pairs (id, source_type, source_id, thread_id, inbound_text,"
        " reply_text, inbound_author, reply_author, paired_at) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(_ins, (
        3, "gmail_thread", "s3", "th-3",
        "A sufficiently long automated notification body for the test.",
        "A long enough reply body here.", "noreply@robot.com", "Me", "2026-06-03",
    ))
    conn.execute(_ins, (
        4, "gmail_thread", "s4", "th-4", "short", "ok", "Carol <c@z.com>", "Me", "2026-06-04",
    ))
    conn.commit()
    conn.close()

    cases = sample_pairs(corpus_db, n=10)
    ids = {c.reply_pair_id for c in cases}
    assert ids == {1, 2}  # automation (3) and trivial (4) excluded


# --- scoring + aggregation -----------------------------------------------------


def _case(**over):
    base = dict(
        reply_pair_id=1, thread_id="t", inbound_author="Alice <a@x.com>",
        inbound_text="Can you confirm Thursday works and send the updated figures?",
        real_reply="Thursday works. Figures attached — let me know if anything is off.",
        paired_at="2026-06-01",
    )
    base.update(over)
    return ReplayCase(**base)


def test_evaluate_case_scores_basic_metrics():
    m = evaluate_case(_case(), "Thursday works for me — I'll send the updated figures shortly.")
    assert 0.0 <= m["voice_match"] <= 1.0
    assert m["lang_match"] is True
    assert m["inbound_has_question"] is True


def test_evaluate_case_flags_language_mismatch():
    m = evaluate_case(
        _case(real_reply="Danke dir, das passt so. Donnerstag funktioniert gut bei mir, bis dahin alles Gute."),
        "Thursday works for me, see you then.",
    )
    assert m["lang_match"] is False


def test_run_replay_with_stub_and_aggregate():
    cases = [_case(reply_pair_id=i) for i in range(1, 4)]

    def _stub(case):
        if case.reply_pair_id == 2:
            # Fabricates a state + commits where the real reply doesn't.
            return ("The figures have been sent. No further action needed. I'll handle it.", "stub")
        return ("Thursday works — sending the updated figures shortly.", "stub")

    results = run_replay(cases, database_url="sqlite:///unused", configs_dir=None, draft_fn=_stub)
    assert len(results) == 3 and all(r.metrics for r in results)

    summary = aggregate(results)
    assert summary["scored"] == 3
    assert summary["issues"]["ungrounded_status_claims"]["count"] == 1
    assert summary["issues"]["ungrounded_status_claims"]["pair_ids"] == [2]
    assert summary["avg_voice_match"] is not None
