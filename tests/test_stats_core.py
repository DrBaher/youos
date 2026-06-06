"""Tests for unified stats query layer."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.core.stats import get_corpus_stats, get_model_status, get_pipeline_status


def test_get_corpus_stats_no_db(tmp_path):
    """Returns zeros when database doesn't exist."""
    result = get_corpus_stats(f"sqlite:///{tmp_path}/nonexistent.db")
    assert result["total_documents"] == 0
    assert result["total_reply_pairs"] == 0
    assert result["total_feedback_pairs"] == 0


def test_verbatim_accepted_pairs_counted_as_subset_of_organic(tmp_path):
    """Agent drafts the user sent unedited are a distinct, observable subset of
    organic backfill — counted, but kept out of the edit-distance metrics (b198)."""
    db_path = tmp_path / "s.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT, generated_draft TEXT, edited_reply TEXT,
            feedback_note TEXT, rating INTEGER, used_in_finetune INTEGER DEFAULT 0,
            edit_distance_pct REAL, reply_pair_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, organic INTEGER DEFAULT 0
        )"""
    )
    conn.executemany(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, feedback_note, edit_distance_pct, organic) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("in1", "draft1", "draft1", "verbatim-accepted (agent draft sent unedited)", 0.0, 1),
            ("in2", "sent2", "sent2", "organic pair — no YouOS draft", 0.0, 1),
            ("in3", "draft3", "edited3", "real draft-vs-sent (prior agent draft)", 0.2, 0),
        ],
    )
    conn.commit()
    conn.close()

    result = get_corpus_stats(f"sqlite:///{db_path}")
    assert result["organic_feedback_pairs"] == 2          # both organic rows
    assert result["verbatim_accepted_pairs"] == 1         # only the verbatim win
    assert result["real_draft_feedback_pairs"] == 1       # the genuine edit, unchanged


def test_get_model_status_no_adapter_with_mlx():
    """No adapter but mlx_lm present → drafts run on the BASE model (not Claude).

    The old code reported "claude" here — the false-confidence bug: with the
    local engine available, a missing adapter means base-model drafting, not a
    cloud fallback.
    """
    from app.core.config import model_label

    with patch("app.core.stats.ADAPTER_PATH", Path("/nonexistent/adapters")), patch("shutil.which", return_value="/usr/bin/mlx_lm"):
        result = get_model_status(Path("/tmp/configs"))
    # b174: the label derives from the configured base model (qwen3-4b-base today)
    # rather than a hardcoded qwen2.5 string. Derive the expected value from the
    # same helper the code uses so this tracks the configured base.
    assert result["generation_model"] == model_label(with_adapter=False)
    assert result["lora_adapter_exists"] is False
    assert result["local_available"] is True


def test_get_model_status_no_mlx_is_claude():
    """No local engine → genuinely the cloud fallback, adapter or not."""
    with patch("app.core.stats.ADAPTER_PATH", Path("/nonexistent/adapters")), patch("shutil.which", return_value=None):
        result = get_model_status(Path("/tmp/configs"))
    assert result["generation_model"] == "claude"
    assert result["local_available"] is False


def test_get_model_status_with_adapter(tmp_path):
    """Adapter present + mlx_lm available → local LoRA."""
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_text("fake")
    from app.core.config import model_label

    with patch("app.core.stats.ADAPTER_PATH", adapter_dir), patch("shutil.which", return_value="/usr/bin/mlx_lm"):
        result = get_model_status(Path("/tmp/configs"))
    # b174: label derives from the configured base model (qwen3-4b-lora today).
    assert result["generation_model"] == model_label(with_adapter=True)
    assert result["lora_adapter_exists"] is True
    assert result["lora_trained_at"] is not None


def test_get_pipeline_status_missing(tmp_path):
    """Returns None when no pipeline log exists."""
    result = get_pipeline_status(tmp_path)
    assert result is None


def test_get_pipeline_status_exists(tmp_path):
    """Returns parsed JSON when pipeline log exists."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    log = {"run_at": "2026-03-16T01:00:00", "status": "ok", "steps": {}, "errors": []}
    (var_dir / "pipeline_last_run.json").write_text(json.dumps(log))
    result = get_pipeline_status(tmp_path)
    assert result["status"] == "ok"


def test_get_pipeline_status_corrupt(tmp_path):
    """Returns None when pipeline log is corrupt."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "pipeline_last_run.json").write_text("not json")
    result = get_pipeline_status(tmp_path)
    assert result is None


def test_get_corpus_stats_outcome_metrics(tmp_path):
    """Computes outcome metrics from REAL draft-vs-sent feedback_pairs (b185).

    The four edit-distance rows here are genuine comparisons (organic=0, draft
    differs from the sent reply), so they all feed the metrics exactly as
    before. The b185 honesty filter only changes which rows qualify — see
    test_get_corpus_stats_excludes_organic for the exclusion behavior."""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.execute(
            """
            CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                generated_draft TEXT,
                edited_reply TEXT,
                edit_distance_pct REAL,
                rating INTEGER,
                organic BOOLEAN DEFAULT 0
            )
            """
        )

        conn.executemany("INSERT INTO documents(id) VALUES(?)", [(1,), (2,)])
        conn.executemany("INSERT INTO reply_pairs(id, embedding) VALUES(?, ?)", [(1, b"x"), (2, None), (3, b"y")])

        # (id, created_at, generated_draft, edited_reply, edit_distance_pct, rating, organic)
        rows = [
            (1, "2026-03-17", "draft1", "sent1", 0.00, 5, 0),
            (2, "2026-03-17", "draft2", "sent2", 0.05, 4, 0),
            (3, "2026-03-16", "draft3", "sent3", 0.20, 3, 0),
            (4, "2026-03-15", "draft4", "sent4", 0.40, 2, 0),
        ]
        conn.executemany(
            "INSERT INTO feedback_pairs(id, created_at, generated_draft, edited_reply, edit_distance_pct, rating, organic) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    result = get_corpus_stats(f"sqlite:///{db_path}")

    assert result["total_documents"] == 2
    assert result["total_reply_pairs"] == 3
    assert result["total_feedback_pairs"] == 4
    assert result["embedding_pct"] == 66.7
    assert result["real_draft_feedback_pairs"] == 4
    assert result["organic_feedback_pairs"] == 0

    outcome = result["outcome_metrics"]
    assert outcome["accept_unchanged_pct"] == 25.0
    assert outcome["low_edit_pct"] == 50.0
    assert outcome["high_rating_pct"] == 50.0
    assert outcome["median_edit_distance"] == 0.125


def test_get_corpus_stats_excludes_organic(tmp_path):
    """b185: organic backfill rows (sent reply copied into both columns, ed=0.0)
    must NOT drive the corpus quality metrics. With many organic 0.0 rows and a
    single real heavily-edited comparison, the metrics reflect ONLY the real row
    (not a fake ~100% accept-unchanged / 0.0 median from the backfill)."""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.execute(
            """
            CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                generated_draft TEXT,
                edited_reply TEXT,
                edit_distance_pct REAL,
                rating INTEGER,
                organic BOOLEAN DEFAULT 0
            )
            """
        )
        # 10 organic 0.0 rows (sent copied into both cols) + 1 real edited row.
        organic = [(i, "2026-03-17", "sent", "sent", 0.0, 3, 1) for i in range(1, 11)]
        real = [(11, "2026-03-17", "agent draft", "what user actually sent", 0.50, 2, 0)]
        conn.executemany(
            "INSERT INTO feedback_pairs(id, created_at, generated_draft, edited_reply, edit_distance_pct, rating, organic) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            organic + real,
        )
        conn.commit()
    finally:
        conn.close()

    result = get_corpus_stats(f"sqlite:///{db_path}")

    assert result["total_feedback_pairs"] == 11
    assert result["real_draft_feedback_pairs"] == 1
    assert result["organic_feedback_pairs"] == 10
    # Only the one real comparison informs quality — NOT the 10 organic 0.0s.
    assert result["avg_edit_distance"] == 0.5
    outcome = result["outcome_metrics"]
    assert outcome["accept_unchanged_pct"] == 0.0  # the single real row was edited
    assert outcome["median_edit_distance"] == 0.5


def test_get_draft_vs_sent_stats(tmp_path, monkeypatch):
    """Drafts-vs-sends panel: send rate, edit distance, worst examples, and a
    bounded threshold recommendation from the captured outcomes."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir()
    (tmp_path / "configs").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "schema.sql").write_text((Path(__file__).resolve().parents[1] / "docs" / "schema.sql").read_text())
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")
    from app.core.config import load_config
    load_config.cache_clear()
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database
    bootstrap_database()
    db_url = f"sqlite:///{tmp_path}/var/youos.db"

    from app.agent import store
    # 5 replied (sent) + 37 unreplied (no_send) — the real prod over-drafting shape.
    for outcome, n in (("sent", 5), ("no_send", 37)):
        for i in range(n):
            rid = store.upsert_pending(
                db_url, message_id=f"m-{outcome}-{i}", thread_id=f"t-{outcome}-{i}",
                account="you@example.com", sender="A <a@x.com>", sender_email="a@x.com",
                subject="s", body="b", received_at="2026-06-01T10:00:00Z",
                needs_reply_score=0.8, reasons=[], cold_outreach=False, tier="draft",
                draft="d", draft_model="qwen", draft_repairs=[], standing_instructions_snapshot=None,
            )
            c = sqlite3.connect(f"{tmp_path}/var/youos.db")
            c.execute("UPDATE agent_pending_drafts SET outcome=?, outcome_captured=1 WHERE id=?", (outcome, rid))
            c.commit()
            c.close()

    # A captured outcome feedback pair (high divergence) tagged with the known note.
    c = sqlite3.connect(f"{tmp_path}/var/youos.db")
    c.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, feedback_note, "
        "rating, used_in_finetune, edit_distance_pct, organic, sender_type) VALUES (?,?,?,?,?,0,?,0,?)",
        ("Can you confirm?", "Yes, confirmed.", "No, let's push to next week instead.",
         "auto: real Gmail send outcome (draft vs what you actually sent)", 2, 0.85, "external_client"),
    )
    c.commit()
    c.close()

    from app.core.stats import get_draft_vs_sent_stats
    r = get_draft_vs_sent_stats(db_url, account="you@example.com")
    assert r["available"] is True
    assert r["sent_total"] == 5 and r["no_send"] == 37
    assert r["send_rate"] is not None and r["send_rate"] < 0.2
    assert r["paired"] == 1
    assert r["high_divergence"] == 1
    assert len(r["worst_examples"]) == 1
    assert "push to next week" in r["worst_examples"][0]["sent"]
    # Over-drafting → recommendation raises the threshold (default 0.6 -> 0.65).
    assert r["recommendation"]["changed"] is True
    assert r["recommendation"]["recommended"] == 0.65
