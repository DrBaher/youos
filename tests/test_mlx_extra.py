"""The local-model engine (MLX) is installable + auto-installed (Apple Silicon).

MLX powers on-device generation/fine-tuning but isn't bundled with macOS and
wasn't installed by YouOS. These pin the `mlx` extra and that install.sh
installs it on Apple Silicon, so a fresh install yields a working local model.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mlx_extra_declared_with_mlx_lm():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "mlx" in extras, "missing the `mlx` extra"
    assert any("mlx-lm" in dep for dep in extras["mlx"]), "mlx extra should pull mlx-lm"


def test_installer_installs_mlx_on_apple_silicon():
    sh = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert '.[mlx]' in sh                       # installs the extra
    assert "arm64" in sh and "Darwin" in sh     # gated to Apple Silicon
