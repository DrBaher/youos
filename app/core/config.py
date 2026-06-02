"""YouOS user configuration loader.

Reads youos_config.yaml and provides typed access to user settings.
All persona-specific values (name, emails, internal domains) are derived from this config.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.secure_io import write_secret

ROOT_DIR = Path(__file__).resolve().parents[2]


def _default_config_path() -> Path:
    data_dir = os.environ.get("YOUOS_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "youos_config.yaml"
    return ROOT_DIR / "youos_config.yaml"


CONFIG_PATH = _default_config_path()


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
    """Get internal domains from explicit config or derive from user emails."""
    cfg = config or load_config()
    # Explicit internal_domains from config takes priority
    explicit = cfg.get("user", {}).get("internal_domains", [])
    if explicit:
        return frozenset(d.lower() for d in explicit if d)

    # Fall back to deriving from email addresses
    emails = get_user_emails(cfg)
    domains: set[str] = set()
    personal = {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "icloud.com",
        "me.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "fastmail.com",
    }
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


def get_ingestion_google_backend(config: dict[str, Any] | None = None) -> str:
    """Which backend fetches Gmail/Docs: ``gog`` (default), ``gws``, or ``native``.

    Surfaced so YouOS can move off the OpenClaw ``gog`` CLI without code
    changes — ``gws`` is Google's own Workspace CLI and ``native`` is the
    direct Google-API client. An unrecognized value degrades to ``gog`` (the
    always-available default) rather than breaking ingestion at config-read
    time; the doctor is responsible for flagging a misconfigured backend.
    """
    cfg = config or load_config()
    raw = cfg.get("ingestion", {}).get("google_backend", "gog")
    value = str(raw).strip().lower() if raw else "gog"
    return value if value in ("gog", "gws", "native") else "gog"


# Repo default base model. The local drafting base migrated from
# Qwen2.5-1.5B to Qwen3-4B-Instruct-2507 (b174): a larger, ChatML/<|im_end|>,
# NON-thinking (no <think> tags), Apache-2.0 model. The mlx 4-bit build is
# ``mlx-community/Qwen3-4B-Instruct-2507-4bit``; per-instance config (e.g.
# baheros) overrides ``model.base`` for the exact weights to load.
DEFAULT_BASE_MODEL = "Qwen/Qwen3-4B-Instruct-2507"

# Dedicated embedding model, DECOUPLED from the drafting base (b177) and now a
# purpose-built retrieval encoder (b180).
#
# History: embeddings used to mean-pool a CAUSAL LM (Qwen2.5-1.5B) — heavy
# (1536-d) and not trained for retrieval. b180 switches the default to a
# dedicated multilingual sentence-embedding model: ``intfloat/multilingual-e5-small``
# (XLM-RoBERTa, 384-d, MIT license, 100 languages incl. German+English — the
# inbox is DE/EN). It is small + fast (~0.1B params) so it runs on every
# retrieval query and indexes the ~30k-row corpus comfortably on a 16GB M4, and
# it loads locally on Apple Silicon via ``mlx_embeddings`` (MLX/Metal). The
# embedder is still NOT ``model.base`` — swapping the drafting base never moves
# the embedding space.
#
# Switching the default changes the embedding *id* and *dimension*; b177 per-row
# ``embedding_model_id`` tagging marks every existing (1.5B / 1536-d) row stale,
# so retrieval ignores cross-space comparisons and a one-time
# ``scripts/index_embeddings.py --reindex`` re-embeds the corpus with the new
# model. See the b180 runbook.
#
# Hosts without ``mlx_embeddings`` (or pinning a Qwen/*-Instruct id) keep using
# the causal-LM mean-pooling fallback in ``app.core.embeddings`` automatically.
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


def get_base_model(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("model", {}).get("base", DEFAULT_BASE_MODEL)


def get_embedding_model(config: dict[str, Any] | None = None) -> str:
    """Resolve the embedding model id, independent of the drafting base (b177).

    Resolution order:
    1. ``model.embedding_model`` in config (explicit pin), then
    2. ``DEFAULT_EMBEDDING_MODEL`` (b180: a dedicated multilingual embedder).

    This intentionally does NOT fall back to ``model.base`` — that coupling is
    exactly the b177 bug. The legacy ``embeddings.model_id`` override (read in
    ``app.core.embeddings.get_embedding_model_id``) still takes precedence over
    this for backward compatibility.
    """
    cfg = config or load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    override = model_cfg.get("embedding_model") if isinstance(model_cfg, dict) else None
    if isinstance(override, str) and override.strip():
        return override.strip()
    return DEFAULT_EMBEDDING_MODEL


def model_label(base: str | None = None, *, with_adapter: bool) -> str:
    """Derive the ``model_used`` telemetry label from the configured base model.

    Turns a HuggingFace base id into a stable, family+size label and appends the
    drafting mode, e.g. ``Qwen/Qwen3-4B-Instruct-2507`` ->
    ``qwen3-4b-lora`` / ``qwen3-4b-base``. Deriving it (rather than hardcoding a
    ``qwen2.5-1.5b-*`` string) means the label tracks the real base after a model
    migration instead of silently lying (b174).

    The label is intentionally coarse — ``_model_family`` buckets on the
    ``-lora`` / ``-base`` suffix, so any base maps cleanly to local-lora /
    local-base for stats.
    """
    base = base or get_base_model()
    short = _short_model_name(base)
    return f"{short}-lora" if with_adapter else f"{short}-base"


def _short_model_name(base: str) -> str:
    """Collapse a HF model id to a short ``<family><size>`` token, e.g.
    ``Qwen/Qwen3-4B-Instruct-2507`` -> ``qwen3-4b``. Best-effort and lossy: it
    only needs to be stable and human-readable for telemetry, not reversible.

    Strategy: take the repo basename, lowercase it, then keep the leading
    ``<letters><digits...>`` family token plus the first ``<num>[bm]`` size token
    if present. Falls back to a sanitized basename when nothing matches."""
    import re

    name = (base or "").split("/")[-1].lower()
    if not name:
        return "model"
    # family token: leading letters + optional version digits, e.g. qwen3, llama3
    fam_m = re.match(r"[a-z]+[0-9.]*", name)
    family = fam_m.group(0).rstrip(".") if fam_m else ""
    # size token: first standalone <number>(b|m), e.g. 4b, 1.5b, 7b, 500m
    size_m = re.search(r"(\d+(?:\.\d+)?)\s*([bm])\b", name)
    size = f"{size_m.group(1)}{size_m.group(2)}" if size_m else ""
    label = "-".join(p for p in (family, size) if p)
    if label:
        return label
    # Fallback: sanitized basename so the label is never empty/misleading.
    return re.sub(r"[^a-z0-9.]+", "-", name).strip("-") or "model"


def get_model_fallback(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("model", {}).get("fallback", "none")


def cloud_escalation_enabled(config: dict[str, Any] | None = None) -> bool:
    """Whether the cloud (Claude) drafting escape hatch is enabled (b175).

    DEFAULT FALSE. When false, drafting is local-only / uses the existing
    fallback chain exactly as before and no inbound mail ever leaves the device
    via the new opt-in lever. When true, Claude MAY draft hard cases, but ONLY
    on explicit interactive draft requests that also opt in
    (``DraftRequest.allow_cloud_escalation``) and NEVER on any background / eval
    path. Reads ``drafting.cloud_escalation.enabled``; fail-closed (any
    malformed config -> False).
    """
    try:
        cfg = config or load_config()
        draft_cfg = cfg.get("drafting", {})
        if not isinstance(draft_cfg, dict):
            return False
        esc = draft_cfg.get("cloud_escalation", {})
        if not isinstance(esc, dict):
            return False
        return bool(esc.get("enabled", False))
    except Exception:
        return False


# Single shared default for the server bind port. The prod launcher
# (scripts/run_youos.sh) and the launchd plist (app/core/service.py) bind here;
# the Origin allowlist (app/core/auth.py) is computed from the SAME value via
# resolve_server_port(), so the allowlist can never drift from the served port
# and silently 403 every authenticated same-origin POST once a PIN is set
# (b165). Keep this in sync with scripts/run_youos.sh's YOUOS_PORT default.
DEFAULT_SERVER_PORT = 8765
DEFAULT_SERVER_HOST = "127.0.0.1"


def resolve_server_port(config: dict[str, Any] | None = None) -> int:
    """The port the server is ACTUALLY served on — the single source of truth.

    Precedence: ``YOUOS_PORT`` env var (what the launcher passes to uvicorn)
    > config ``server.port`` > ``DEFAULT_SERVER_PORT``. Because the bind path
    (run_youos.sh / cli serve / launchd plist) and the Origin allowlist both
    resolve the port through here, the allowlist always matches the bind port.
    A malformed ``YOUOS_PORT`` falls through to the config/default rather than
    crashing the allowlist computation."""
    env_port = os.environ.get("YOUOS_PORT")
    if env_port:
        try:
            return int(env_port)
        except (TypeError, ValueError):
            pass
    if isinstance(config, dict):
        raw = config.get("server", {}).get("port")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        return DEFAULT_SERVER_PORT
    cfg = load_config()
    try:
        return int(cfg.get("server", {}).get("port", DEFAULT_SERVER_PORT))
    except (TypeError, ValueError):
        return DEFAULT_SERVER_PORT


def resolve_server_host(config: dict[str, Any] | None = None) -> str:
    """The host the server is ACTUALLY bound to — single source of truth.

    Precedence: ``YOUOS_HOST`` env var (what the launcher passes to uvicorn)
    > config ``server.host`` > ``DEFAULT_SERVER_HOST``."""
    env_host = (os.environ.get("YOUOS_HOST") or "").strip()
    if env_host:
        return env_host
    cfg = config if isinstance(config, dict) else load_config()
    return cfg.get("server", {}).get("host", DEFAULT_SERVER_HOST) or DEFAULT_SERVER_HOST


def get_server_port(config: dict[str, Any] | None = None) -> int:
    return resolve_server_port(config)


def get_server_host(config: dict[str, Any] | None = None) -> str:
    return resolve_server_host(config)


def get_tailscale_hostname(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    return cfg.get("tailscale", {}).get("hostname", "")


def get_user_timezone(config: dict[str, Any] | None = None) -> str:
    """Return ``user.timezone`` (IANA name) or ``UTC`` when unset/invalid.

    Used by ingestion paths that need to attach tzinfo to naive timestamps
    (WhatsApp exports record local times with no offset). Falling back to
    UTC rather than tzlocal() because the server's clock may be in a
    different zone than the device that produced the export — UTC at
    least makes the timestamps monotonic across sources, even if the
    wall-clock interpretation is wrong by a few hours.
    """
    cfg = config or load_config()
    tz = (cfg.get("user", {}) or {}).get("timezone", "") or ""
    if not isinstance(tz, str) or not tz.strip():
        return "UTC"
    return tz.strip()


def get_autoresearch_iterations(config: dict[str, Any] | None = None) -> int:
    cfg = config or load_config()
    return int(cfg.get("autoresearch", {}).get("iterations", 80))


def get_persona_mode_config(sender_type: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    return cfg.get("persona", {}).get("modes", {}).get(sender_type, {})


def get_persona_style_anchor(sender_type: str, config: dict[str, Any] | None = None) -> str | None:
    mode_config = get_persona_mode_config(sender_type, config)
    return mode_config.get("style_anchor")


def get_ollama_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    return cfg.get("model", {}).get("ollama", {})


def is_ollama_enabled(config: dict[str, Any] | None = None) -> bool:
    return bool(get_ollama_config(config).get("enabled", False))


def get_review_batch_size(config: dict[str, Any] | None = None) -> int:
    """Read review.batch_size from config, default 10, clamped to 5-50."""
    cfg = config or load_config()
    raw = cfg.get("review", {}).get("batch_size", 10)
    return max(5, min(50, int(raw)))


def get_review_draft_model(config: dict[str, Any] | None = None) -> str:
    """Read review.draft_model from config.

    'claude'  — use Claude CLI
    'local'   — use local Qwen adapter (private)
    'auto'    — (default) use local if an adapter is trained, else Claude
    """
    cfg = config or load_config()
    val = cfg.get("review", {}).get("draft_model", "auto").lower().strip()
    if val not in ("claude", "local", "auto"):
        return "auto"
    return val


def get_last_ingest_at(account: str, config: dict[str, Any] | None = None) -> str | None:
    cfg = config or load_config()
    return cfg.get("ingestion", {}).get("last_ingest_at", {}).get(account)


def set_last_ingest_at(account: str, timestamp: str, config: dict[str, Any] | None = None) -> None:
    cfg = config if config is not None else _load_raw_config()
    cfg.setdefault("ingestion", {}).setdefault("last_ingest_at", {})[account] = timestamp
    save_config(cfg)


_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "icloud.com",
        "me.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "fastmail.com",
    }
)


def get_account_for_sender(sender: str, config: dict[str, Any] | None = None) -> str | None:
    """Infer which user account email to use based on sender domain.

    - If sender domain matches an internal domain → return work account email
    - If sender is from a personal domain (gmail, yahoo, etc) → return personal account email
    - If ambiguous → return None (use all accounts)
    """
    if not sender or "@" not in sender:
        return None

    cfg = config or load_config()
    emails = get_user_emails(cfg)
    if not emails:
        return None

    sender_domain = sender.rsplit("@", 1)[-1].lower()
    internal_domains = get_internal_domains(cfg)

    # Sender is from an internal domain → use work email (non-personal domain email)
    if sender_domain in internal_domains:
        for email in emails:
            domain = email.split("@", 1)[-1].lower() if "@" in email else ""
            if domain not in _PERSONAL_DOMAINS:
                return email
        return emails[0] if emails else None

    # Sender is from a personal domain → use personal email
    if sender_domain in _PERSONAL_DOMAINS:
        for email in emails:
            domain = email.split("@", 1)[-1].lower() if "@" in email else ""
            if domain in _PERSONAL_DOMAINS:
                return email
        return emails[0] if emails else None

    # External/ambiguous domain → return None (no filter)
    return None


def save_config(config: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or CONFIG_PATH
    # 0o600: youos_config.yaml holds the PBKDF2 PIN hash (a short PIN brute-forces
    # offline in seconds), so it must not be world-readable.
    write_secret(
        path,
        yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
    )
    load_config.cache_clear()
