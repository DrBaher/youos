"""PIN-based authentication for YouOS web UI."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

SESSIONS_PATH = Path(__file__).resolve().parents[2] / "var" / "sessions.json"
SESSION_MAX_AGE = 86400  # 24 hours


def get_pin_hash(pin: str) -> str:
    """Hash a PIN using SHA-256."""
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def verify_pin(pin: str, stored_hash: str) -> bool:
    """Verify a PIN against a stored hash."""
    return secrets.compare_digest(get_pin_hash(pin), stored_hash)


def is_auth_enabled(config: dict[str, Any]) -> bool:
    """Check if PIN auth is enabled (non-empty pin hash in config)."""
    pin_value = config.get("server", {}).get("pin", "")
    return bool(pin_value)


def create_session_token() -> str:
    """Create a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def load_sessions(path: Path | None = None) -> dict[str, float]:
    """Load sessions from JSON file, prune expired tokens.

    Returns dict of {token: created_at_unix}.
    """
    if path is None:
        path = SESSIONS_PATH
    now = time.time()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        # Prune expired
        return {tok: ts for tok, ts in data.items() if now - ts < SESSION_MAX_AGE}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_sessions(sessions: dict[str, float], path: Path | None = None) -> None:
    """Write sessions dict to JSON file."""
    if path is None:
        path = SESSIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions), encoding="utf-8")


def persist_new_session(token: str, path: Path | None = None) -> None:
    """Add a new session token and persist to disk."""
    sessions = load_sessions(path)
    sessions[token] = time.time()
    save_sessions(sessions, path)


class LoginRateLimiter:
    """Simple rate limiter: 3 attempts then 60s lockout per IP."""

    def __init__(self, max_attempts: int = 3, lockout_seconds: int = 60):
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = {}

    def is_locked(self, client_ip: str) -> bool:
        attempts = self._attempts.get(client_ip, [])
        if len(attempts) < self.max_attempts:
            return False
        last_attempt = attempts[-1]
        return (time.time() - last_attempt) < self.lockout_seconds

    def record_attempt(self, client_ip: str) -> None:
        if client_ip not in self._attempts:
            self._attempts[client_ip] = []
        self._attempts[client_ip].append(time.time())
        # Keep only recent attempts
        cutoff = time.time() - self.lockout_seconds
        self._attempts[client_ip] = [
            t for t in self._attempts[client_ip] if t > cutoff
        ]

    def reset(self, client_ip: str) -> None:
        self._attempts.pop(client_ip, None)
