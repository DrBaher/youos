"""Doctor checks for YouOS system health."""

from __future__ import annotations

import importlib
import os
import shutil
import socket
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def run_doctor_checks() -> tuple[bool, list[str]]:
    """Run system health checks.

    Returns (all_required_passed, list_of_failure_messages_for_required_only).
    """
    failures: list[str] = []

    # Required: Python >= 3.11
    if sys.version_info < (3, 11):
        failures.append(f"Python >= 3.11 required (have {sys.version_info.major}.{sys.version_info.minor})")

    # Required: gog CLI installed
    if shutil.which("gog") is None:
        failures.append("gog CLI not found in PATH")

    # Required: mlx_lm importable
    try:
        importlib.import_module("mlx_lm")
    except ImportError:
        failures.append("mlx_lm not importable (pip install mlx-lm)")

    # Required: youos_config.yaml exists
    config_path = ROOT_DIR / "youos_config.yaml"
    if not config_path.exists():
        failures.append("youos_config.yaml not found")

    # Required: user.emails set
    try:
        from app.core.config import get_user_emails

        emails = get_user_emails()
        if not emails:
            failures.append("user.emails not set in config")
    except Exception:
        failures.append("user.emails not set in config")

    all_passed = len(failures) == 0
    return (all_passed, failures)


def run_doctor_checks_full() -> tuple[bool, list[str], list[str]]:
    """Run all checks including warnings.

    Returns (all_required_passed, required_failures, warnings).
    """
    passed, failures = run_doctor_checks()
    warnings: list[str] = []

    # Warning: var/youos.db exists
    db_path = ROOT_DIR / "var" / "youos.db"
    if not db_path.exists():
        warnings.append("var/youos.db not found")

    # Warning: >= 3GB disk free
    try:
        stat = os.statvfs(ROOT_DIR)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        if free_gb < 3.0:
            warnings.append(f"Low disk space ({free_gb:.1f}GB free, recommend >= 3GB)")
    except Exception:
        warnings.append("Could not check disk space")

    # Warning: models/ has content
    models_dir = ROOT_DIR / "models"
    if not models_dir.exists() or not any(models_dir.iterdir()):
        warnings.append("models/ directory is empty")

    # Warning: port 8901 free
    try:
        from app.core.config import get_server_port

        port = get_server_port()
    except Exception:
        port = 8901
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.close()
    except OSError:
        warnings.append(f"Port {port} is in use")

    return (passed, failures, warnings)
