"""Atomic plain-file writes for non-secret state files (b241).

State files like pipeline_last_run.json, persona.yaml, golden_results.json
and train.jsonl are written by one process (nightly, CLI) and read by
another (the live server) — and must survive a crash/power-loss mid-write.
A truncate-in-place write exposes both a torn read and a torn file: a
partial JSON/YAML often still PARSES, silently dropping keys (e.g. the
adapter-promotion baseline, or every persona style pattern).

Secrets (0o600) go through ``app.core.secure_io.write_secret`` instead.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via same-dir temp + fsync + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0o600; restore the conventional non-secret mode so
        # replacing an existing 0o644 file doesn't silently tighten it.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    """Serialize ``obj`` and atomically write it to ``path``."""
    atomic_write_text(path, json.dumps(obj, indent=indent))
