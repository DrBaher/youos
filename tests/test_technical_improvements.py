"""Tests for Items 6-9: Ollama, embedding indexer, incremental ingestion, dedup."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner

from app.core.config import (
    get_last_ingest_at,
    get_ollama_config,
    is_ollama_enabled,
    set_last_ingest_at,
)
from scripts.youos_cli import app

runner = CliRunner()


# --- Item 6: Ollama backend ---

def test_generate_via_ollama_returns_string():
    """Ollama backend returns a string (mock urllib)."""
    from app.generation.service import _generate_via_ollama

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"response": "Hello there!"}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _generate_via_ollama("test prompt", model="mistral")
    assert isinstance(result, str)
    assert result == "Hello there!"


def test_ollama_config_defaults():
    """Ollama config returns empty dict when model section has no ollama key."""
    assert get_ollama_config({"model": {}}) == {}
    assert not is_ollama_enabled({"model": {}})


def test_ollama_config_enabled():
    """Ollama enabled detection works."""
    config = {"model": {"ollama": {"enabled": True, "model": "mistral"}}}
    assert is_ollama_enabled(config)
    assert get_ollama_config(config)["model"] == "mistral"


def test_status_shows_ollama_field():
    """youos status shows Ollama field."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Ollama:" in result.output


# --- Item 7: Embedding indexer in nightly pipeline ---

def test_nightly_pipeline_includes_embedding_step():
    """Nightly pipeline has step_index_embeddings function."""
    from scripts.nightly_pipeline import step_index_embeddings
    assert callable(step_index_embeddings)


# --- Item 8: Incremental ingestion ---

def test_get_last_ingest_at_returns_none_when_not_set():
    """get_last_ingest_at returns None for unknown accounts."""
    config = {"ingestion": {"last_ingest_at": {}}}
    assert get_last_ingest_at("unknown@example.com", config) is None


def test_get_last_ingest_at_returns_none_empty_config():
    """get_last_ingest_at returns None with empty config."""
    assert get_last_ingest_at("test@example.com", {}) is None


def test_set_last_ingest_at_updates_config(tmp_path):
    """set_last_ingest_at writes timestamp to config file."""
    config_path = tmp_path / "youos_config.yaml"
    config = {"ingestion": {"accounts": ["a@b.com"], "last_ingest_at": {}}}
    config_path.write_text(yaml.dump(config), encoding="utf-8")

    with patch("app.core.config.CONFIG_PATH", config_path):
        with patch("app.core.config.load_config.cache_clear"):
            set_last_ingest_at("a@b.com", "2026-03-14T10:00:00Z")

    reloaded = yaml.safe_load(config_path.read_text())
    assert reloaded["ingestion"]["last_ingest_at"]["a@b.com"] == "2026-03-14T10:00:00Z"


# --- Item 9: Corpus deduplication ---

def _setup_dedup_db(db_path: Path) -> None:
    """Create a test DB with duplicate rows."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE reply_pairs ("
        "id INTEGER PRIMARY KEY, source_type TEXT, source_id TEXT, "
        "thread_id TEXT, inbound_text TEXT, inbound_author TEXT, "
        "reply_text TEXT, paired_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE documents ("
        "id INTEGER PRIMARY KEY, source_type TEXT, source_id TEXT, "
        "content TEXT, embedding BLOB)"
    )
    # Insert duplicate reply_pairs (same source_type + source_id)
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, thread_id, inbound_text) "
        "VALUES ('gmail', 'msg-1', 't1', 'hello')"
    )
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, thread_id, inbound_text) "
        "VALUES ('gmail', 'msg-1', 't1', 'hello')"
    )
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, thread_id, inbound_text) "
        "VALUES ('gmail', 'msg-2', 't2', 'world')"
    )
    # Insert duplicate documents
    conn.execute(
        "INSERT INTO documents (source_type, source_id, content) "
        "VALUES ('gmail', 'doc-1', 'content A')"
    )
    conn.execute(
        "INSERT INTO documents (source_type, source_id, content) "
        "VALUES ('gmail', 'doc-1', 'content A')"
    )
    conn.commit()
    conn.close()


def test_deduplicate_dry_run(tmp_path):
    """Dry-run shows duplicates without deleting."""
    db_path = tmp_path / "youos.db"
    _setup_dedup_db(db_path)

    mock_settings = MagicMock()
    mock_settings.database_url = f"sqlite:///{db_path}"

    with patch("scripts.deduplicate_corpus.get_settings", return_value=mock_settings):
        with patch("scripts.deduplicate_corpus.resolve_sqlite_path", return_value=db_path):
            from scripts.deduplicate_corpus import deduplicate
            result = deduplicate(dry_run=True)

    assert result["reply_pairs"] >= 1
    assert result["documents"] >= 1
    assert result["total"] == 0  # dry run doesn't delete

    # Verify rows still exist
    conn = sqlite3.connect(db_path)
    pairs = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert pairs == 3
    assert docs == 2
