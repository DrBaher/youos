"""Corpus quality: signature-only reply detection + consumer filters (b235-b237).

The replay backtest found ~18% of sampled reply pairs had a "reply" that was
just the user's signature block or "FYI." + signature — poisoning fine-tuning,
retrieval exemplars, and eval ground truth. One detector, applied at
ingestion, cleanup, and sampling.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.pair_quality import reply_content, signature_only_reply

_SIG = (
    "Sam Example CEO / Example AI w: example.ai e: sam@example.ai m: +43 660 0000000 "
    "Connect with us on: Linkedin | Medium | Twitter"
)


def test_bare_signature_is_junk():
    assert signature_only_reply(_SIG, reply_author="Sam Example <sam@example.ai>")


def test_fyi_plus_signature_is_junk():
    assert signature_only_reply(f"FYI. {_SIG}", reply_author="Sam Example <sam@example.ai>")


def test_terse_real_reply_with_signature_is_kept():
    # "Signed papers." is a real (if terse) reply — must NOT be junk.
    assert not signature_only_reply(
        f"Signed papers. {_SIG}", reply_author="Sam Example <sam@example.ai>"
    )


def test_normal_reply_is_kept():
    assert not signature_only_reply(
        "Thursday works for me — I'll send the updated figures after the call.",
        reply_author="Sam Example <sam@example.ai>",
    )


def test_name_mention_in_body_does_not_truncate():
    # The user's name in prose (no title/contact furniture after it) is body
    # text, not a signature start.
    text = "Sam Example will join the call on Thursday and walk through the figures in detail."
    assert reply_content(text, author_names=["Sam Example"]) == text
    assert not signature_only_reply(text, reply_author="Sam Example <sam@example.ai>")


def test_user_names_param_works_without_author():
    assert signature_only_reply(f"Noted. {_SIG}", user_names=["Sam Example"])


def test_empty_reply_is_junk():
    assert signature_only_reply("", reply_author="Sam <s@x.com>")
    assert signature_only_reply(None, reply_author="Sam <s@x.com>")


# --- ingestion prevention -----------------------------------------------------


def test_ingestion_skips_signature_only_reply():
    from app.ingestion.gmail_threads import _is_low_quality_reply

    assert _is_low_quality_reply(f"FYI. {_SIG}", reply_author="Sam Example <sam@example.ai>")
    assert not _is_low_quality_reply(
        "Thanks — confirming Thursday at 14:00, I'll bring the figures.",
        reply_author="Sam Example <sam@example.ai>",
    )


# --- retrieval excludes demoted pairs --------------------------------------------


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

    conn = sqlite3.connect(tmp_path / "var" / "youos.db")
    ins = (
        "INSERT INTO reply_pairs (id, source_type, source_id, thread_id, inbound_text,"
        " reply_text, inbound_author, reply_author, paired_at, quality_score)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(ins, (
        1, "gmail_thread", "s1", "t1",
        "Could you confirm the quarterly pricing for the Vienna rollout?",
        "Confirmed — quarterly pricing unchanged.", "Alice <a@x.com>", "Me <m@x.com>",
        "2026-06-01", 1.0,
    ))
    conn.execute(ins, (
        2, "gmail_thread", "s2", "t2",
        "Could you confirm the quarterly pricing for the Vienna rollout again?",
        _SIG, "Bot <noreply@x.com>", "Me <m@x.com>", "2026-06-02", 0.0,
    ))
    conn.commit()
    conn.close()
    return f"sqlite:///{tmp_path}/var/youos.db"


def test_retrieval_skips_quality_zero_pairs(corpus_db, tmp_path):
    from app.retrieval.service import RetrievalRequest, RetrievalService

    svc = RetrievalService.from_database_url(database_url=corpus_db, configs_dir=tmp_path / "configs")
    resp = svc.retrieve(RetrievalRequest(query="quarterly pricing Vienna rollout"))
    ids = {m.reply_pair_id for m in resp.reply_pairs}
    assert 1 in ids and 2 not in ids


def test_replay_sampling_skips_quality_zero(corpus_db):
    from app.evaluation.replay import sample_pairs

    cases = sample_pairs(corpus_db, n=10, triage_filter=False)
    assert {c.reply_pair_id for c in cases} == {1}


# --- per-sender reply language (b237) --------------------------------------------


def test_majority_reply_language():
    import importlib

    bsp = importlib.import_module("scripts.build_sender_profiles")
    de = "Danke dir, das passt gut. Wir sehen uns am Donnerstag und besprechen alles weitere."
    en = "Thanks, that works well. See you Thursday to discuss the remaining details."
    assert bsp._majority_reply_language([en, en, de]) == "en"
    assert bsp._majority_reply_language([de, de, en]) == "de"
    assert bsp._majority_reply_language([en]) is None  # one vote isn't a habit
    assert bsp._majority_reply_language([en, de]) is None  # tie isn't a majority
    assert bsp._majority_reply_language(["ok", "ja"]) is None  # too short to vote


def test_verify_draft_expected_language_overrides_inbound():
    from app.generation.verify import verify_draft

    inbound_fr = "Bonjour, plusieurs appels d'offres correspondent à votre profil de veille des marchés publics."
    draft_en = "No need to monitor these anymore — we are closing the French entity, so please stop the alerts."
    # Without the override: mismatch blocks.
    assert not verify_draft(draft_en, inbound=inbound_fr).ok
    # With the per-sender habit: English is the INTENDED language.
    assert verify_draft(draft_en, inbound=inbound_fr, expected_language="en").ok
