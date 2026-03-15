"""Tests for YouOS user configuration loading."""
from pathlib import Path
import tempfile

import yaml

from app.core.config import (
    _load_raw_config,
    get_user_name,
    get_display_name,
    get_user_emails,
    get_user_names,
    get_internal_domains,
    get_ingestion_accounts,
    get_base_model,
    get_server_port,
    save_config,
)


def _make_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "youos_config.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def test_load_missing_config(tmp_path):
    config = _load_raw_config(tmp_path / "nonexistent.yaml")
    assert config == {}


def test_load_valid_config(tmp_path):
    data = {"user": {"name": "Alice", "emails": ["alice@example.com"]}}
    path = _make_config(tmp_path, data)
    config = _load_raw_config(path)
    assert config["user"]["name"] == "Alice"


def test_get_user_name_default():
    assert get_user_name({}) == "User"


def test_get_user_name_configured():
    config = {"user": {"name": "Bob"}}
    assert get_user_name(config) == "Bob"


def test_get_display_name_default():
    assert get_display_name({}) == "YouOS"


def test_get_display_name_configured():
    config = {"user": {"display_name": "BobOS"}}
    assert get_display_name(config) == "BobOS"


def test_get_user_emails_empty():
    assert get_user_emails({}) == ()


def test_get_user_emails_configured():
    config = {"user": {"emails": ["a@b.com", "c@d.com"]}}
    assert get_user_emails(config) == ("a@b.com", "c@d.com")


def test_get_user_names():
    config = {"user": {"names": ["Alice", "Alice Smith"]}}
    assert get_user_names(config) == ("Alice", "Alice Smith")


def test_get_internal_domains():
    config = {"user": {"emails": ["alice@company.io", "alice@gmail.com"]}}
    domains = get_internal_domains(config)
    assert "company.io" in domains
    assert "gmail.com" not in domains


def test_get_internal_domains_empty():
    assert get_internal_domains({}) == frozenset()


def test_get_ingestion_accounts_from_emails():
    config = {"user": {"emails": ["a@b.com"]}, "ingestion": {}}
    assert get_ingestion_accounts(config) == ("a@b.com",)


def test_get_ingestion_accounts_explicit():
    config = {"user": {"emails": ["a@b.com"]}, "ingestion": {"accounts": ["x@y.com"]}}
    assert get_ingestion_accounts(config) == ("x@y.com",)


def test_get_base_model_default():
    assert get_base_model({}) == "Qwen/Qwen2.5-1.5B-Instruct"


def test_get_server_port_default():
    assert get_server_port({}) == 8765


def test_save_and_reload(tmp_path):
    path = tmp_path / "test_config.yaml"
    config = {"user": {"name": "Test", "emails": ["t@e.com"]}}
    save_config(config, path)
    reloaded = _load_raw_config(path)
    assert reloaded["user"]["name"] == "Test"
