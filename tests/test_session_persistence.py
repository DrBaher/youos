"""Tests for session token persistence."""

import json
import time

from app.core.auth import load_sessions, persist_new_session, save_sessions


def test_save_and_load_sessions(tmp_path):
    path = tmp_path / "sessions.json"
    sessions = {"tok1": time.time(), "tok2": time.time()}
    save_sessions(sessions, path)
    loaded = load_sessions(path)
    assert "tok1" in loaded
    assert "tok2" in loaded


def test_load_prunes_expired(tmp_path):
    path = tmp_path / "sessions.json"
    now = time.time()
    sessions = {
        "fresh": now,
        "expired": now - 100000,  # >86400s ago
    }
    save_sessions(sessions, path)
    loaded = load_sessions(path)
    assert "fresh" in loaded
    assert "expired" not in loaded


def test_load_missing_file(tmp_path):
    path = tmp_path / "nonexistent.json"
    loaded = load_sessions(path)
    assert loaded == {}


def test_load_corrupt_file(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not json!!!")
    loaded = load_sessions(path)
    assert loaded == {}


def test_persist_new_session(tmp_path):
    path = tmp_path / "sessions.json"
    persist_new_session("token_a", path)
    persist_new_session("token_b", path)
    loaded = load_sessions(path)
    assert "token_a" in loaded
    assert "token_b" in loaded


def test_persist_creates_parent_dir(tmp_path):
    path = tmp_path / "subdir" / "sessions.json"
    persist_new_session("tok", path)
    assert path.exists()
    loaded = load_sessions(path)
    assert "tok" in loaded
