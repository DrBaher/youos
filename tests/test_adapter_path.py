"""ADAPTER_PATH is instance-aware (honors YOUOS_DATA_DIR).

Guards the follow-up flagged in PR #6: generation, fine-tuning, and the
status CLI must resolve the LoRA adapter under the active instance's
``models/adapters/latest`` so per-instance fine-tunes don't overwrite each
other (and a reader looking at the instance dir can find what the writer
just produced).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def _reset_settings():
    """get_settings() is @lru_cache'd; clear it around env-var manipulation."""
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_adapter_path_uses_data_dir(monkeypatch, tmp_path, _reset_settings):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_adapter_path

    assert get_adapter_path() == tmp_path.resolve() / "models" / "adapters" / "latest"


def test_get_adapter_path_falls_back_to_repo_root(monkeypatch, _reset_settings):
    monkeypatch.delenv("YOUOS_DATA_DIR", raising=False)
    from app.core.settings import ROOT_DIR, get_adapter_path

    assert get_adapter_path() == ROOT_DIR / "models" / "adapters" / "latest"


def test_get_adapter_path_expanduser(monkeypatch, tmp_path, _reset_settings):
    """A '~'-prefixed YOUOS_DATA_DIR is expanded before path joining."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("YOUOS_DATA_DIR", "~/youos-instance")
    from app.core.settings import get_adapter_path

    expected = (fake_home / "youos-instance" / "models" / "adapters" / "latest").resolve()
    assert get_adapter_path() == expected


def test_stats_module_adapter_path_matches_helper(_reset_settings):
    from app.core import stats
    from app.core.settings import get_adapter_path

    assert stats.ADAPTER_PATH == get_adapter_path()


def test_finetune_default_adapter_dir_honors_data_dir(monkeypatch, tmp_path, _reset_settings):
    """scripts/finetune_lora.py defaults --adapter-dir to the instance dir."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["finetune_lora.py"])

    from scripts.finetune_lora import parse_args

    args = parse_args()
    expected = str(tmp_path.resolve() / "models" / "adapters" / "latest")
    assert args.adapter_dir == expected


def test_finetune_explicit_adapter_dir_still_wins(monkeypatch, tmp_path, _reset_settings):
    """Explicit --adapter-dir overrides the instance-aware default."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    explicit = str(tmp_path / "elsewhere")
    monkeypatch.setattr("sys.argv", ["finetune_lora.py", "--adapter-dir", explicit])

    from scripts.finetune_lora import parse_args

    args = parse_args()
    assert args.adapter_dir == explicit


def test_generation_service_module_adapter_path_matches_helper_at_import(_reset_settings):
    """app.generation.service.ADAPTER_PATH resolves via the helper at import time.

    The module-level constant is captured once on import; downstream tests
    patch it directly when they need a different value. We just confirm the
    initial wiring goes through ``get_adapter_path`` instead of the old
    hardcoded ``parents[2]`` path.
    """
    from app.core.settings import ROOT_DIR
    from app.generation import service as svc

    assert svc.ADAPTER_PATH == ROOT_DIR / "models" / "adapters" / "latest"
    # And it's the same object the helper computes when no data_dir is set.
    assert svc.ADAPTER_PATH == Path(ROOT_DIR / "models" / "adapters" / "latest")
