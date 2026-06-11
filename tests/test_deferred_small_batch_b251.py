"""b251: small deferred items — config .bak + fail-closed parse error with a
recovery hint, atomic adapter meta.json, VACUUM freelist gate.
"""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from app.core.config import _load_raw_config, save_config


def test_save_config_keeps_previous_version_as_bak(tmp_path):
    cfg_path = tmp_path / "youos_config.yaml"
    save_config({"user": {"name": "A"}}, cfg_path)
    save_config({"user": {"name": "B"}}, cfg_path)
    bak = tmp_path / "youos_config.yaml.bak"
    assert bak.exists()
    assert "name: A" in bak.read_text()  # the PREVIOUS version
    assert "name: B" in cfg_path.read_text()
    # the .bak holds the PIN hash too — must be owner-only like the original
    assert oct(stat.S_IMODE(os.stat(bak).st_mode)) == "0o600"


def test_damaged_config_fails_closed_with_bak_hint(tmp_path):
    cfg_path = tmp_path / "youos_config.yaml"
    save_config({"server": {"pin": "hash"}}, cfg_path)
    save_config({"server": {"pin": "hash2"}}, cfg_path)  # creates .bak
    cfg_path.write_text("server: [unclosed")  # hand-edit gone wrong
    with pytest.raises(ValueError, match=r"\.bak"):
        _load_raw_config(cfg_path)


def test_damaged_config_without_bak_still_fails_closed(tmp_path):
    cfg_path = tmp_path / "youos_config.yaml"
    cfg_path.write_text("server: [unclosed")
    with pytest.raises(ValueError, match="not valid YAML"):
        _load_raw_config(cfg_path)


def test_vacuum_skipped_for_small_freelist(tmp_path, monkeypatch):
    """A prune freeing only a few pages must not trigger a whole-file VACUUM."""
    from app.agent import store
    from app.db.bootstrap import _migrate_agent_pending_drafts

    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, needs_reply_score, "
        "tier, status, created_at) VALUES ('m', 't', 'a@x.com', 0.5, 'surface', 'pending', "
        "datetime('now', '-100 days'))"
    )
    conn.commit()
    conn.close()

    vacuumed = {"n": 0}
    real_connect = store._connect

    def spying_connect(url):
        conn = real_connect(url)

        class _Spy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a):
                if "VACUUM" in str(sql).upper():
                    vacuumed["n"] += 1
                return conn.execute(sql, *a)

        return _Spy()

    monkeypatch.setattr(store, "_connect", spying_connect)
    removed = store.prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_pending_drafts_surface"] == 1
    assert removed["vacuum_ok"] == 1  # nothing wrong — just not worth a rewrite
    assert vacuumed["n"] == 0  # freelist below threshold → no VACUUM


def test_vacuum_runs_when_freelist_large(tmp_path, monkeypatch):
    from app.agent import store
    from app.db.bootstrap import _migrate_agent_pending_drafts

    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, needs_reply_score, "
        "tier, status, created_at) VALUES ('m', 't', 'a@x.com', 0.5, 'surface', 'pending', "
        "datetime('now', '-100 days'))"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store, "VACUUM_MIN_FREELIST_PAGES", 0)  # always over threshold
    removed = store.prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["vacuum_ok"] == 1


def test_adapter_meta_written_atomically(tmp_path):
    """Source-level: meta.json goes through atomic_write_json (a torn meta is
    treated as legacy-allow, silently forfeiting the b174 cross-base guard)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "finetune_lora.py").read_text()
    writer_region = src.split('meta_path = adapter_dir / "meta.json"')[1][:400]
    assert "atomic_write_json(meta_path, meta)" in writer_region
