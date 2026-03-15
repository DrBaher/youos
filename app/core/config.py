"""YouOS user configuration loader.

Reads youos_config.yaml and provides typed access to user settings.
All persona-specific values (name, emails, internal domains) are derived from this config.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "youos_config.yaml"


def _load_raw_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or CONFIG_PATH
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def load_config(config_path: Path | None = None) -> dict[str, Any]:
    return _load_raw_config(config_path)


def get_user_name(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("user", {}).get("name", "") or "User"


def get_display_name(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("user", {}).get("display_name", "") or "YouOS"


def get_user_emails(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    cfg = config or load_config()
    emails = cfg.get("user", {}).get("emails", [])
    return tuple(emails) if emails else ()


def get_user_names(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    cfg = config or load_config()
    names = cfg.get("user", {}).get("names", [])
    return tuple(names) if names else ()


def get_internal_domains(config: dict[str, Any] | None = None) -> frozenset[str]:
    """Derive internal domains from user email addresses."""
    emails = get_user_emails(config)
    domains: set[str] = set()
    personal = {"gmail.com", "yahoo.com", "hotmail.com", "icloud.com",
                "me.com", "outlook.com", "live.com", "aol.com",
                "protonmail.com", "proton.me", "fastmail.com"}
    for email in emails:
        if "@" in email:
            domain = email.split("@", 1)[1].lower()
            if domain not in personal:
                domains.add(domain)
    return frozenset(domains)


def get_ingestion_accounts(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    cfg = config or load_config()
    accounts = cfg.get("ingestion", {}).get("accounts", [])
    if accounts:
        return tuple(accounts)
    return get_user_emails(cfg)


def get_base_model(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("model", {}).get("base", "Qwen/Qwen2.5-1.5B-Instruct")


def get_model_fallback(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("model", {}).get("fallback", "claude")


def get_server_port(config: dict[str, Any] | None = None) -> int:
    cfg = config or load_config()
    return int(cfg.get("server", {}).get("port", 8901))


def get_server_host(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("server", {}).get("host", "0.0.0.0")


def get_tailscale_hostname(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("tailscale", {}).get("hostname", "")


def get_autoresearch_iterations(config: dict[str, Any] | None = None) -> int:
    cfg = config or load_config()
    return int(cfg.get("autoresearch", {}).get("iterations", 80))


def save_config(config: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or CONFIG_PATH
    path.write_text(
        yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    load_config.cache_clear()
