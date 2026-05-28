"""Tests for clawhub.json metadata."""

import json
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]


def test_clawhub_json_exists():
    path = ROOT_DIR / "clawhub.json"
    assert path.exists(), "clawhub.json should exist at repo root"


def test_clawhub_json_valid():
    path = ROOT_DIR / "clawhub.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_clawhub_required_fields():
    path = ROOT_DIR / "clawhub.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    required = ["name", "version", "displayName", "description", "author", "homepage", "category", "tags", "requires", "emoji", "license"]
    for field in required:
        assert field in data, f"Missing required field: {field}"


def test_clawhub_requires_fields():
    path = ROOT_DIR / "clawhub.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    requires = data["requires"]

    assert "bins" in requires
    assert "python3" in requires["bins"]
    assert "gog" in requires["bins"]
    assert requires["platform"] == "darwin"
    assert requires["arch"] == "arm64"


def test_clawhub_version_matches_pyproject():
    clawhub = json.loads((ROOT_DIR / "clawhub.json").read_text(encoding="utf-8"))
    pyproject = (ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{clawhub["version"]}"' in pyproject


def test_clawhub_tags_are_strings():
    path = ROOT_DIR / "clawhub.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["tags"], list)
    assert all(isinstance(t, str) for t in data["tags"])


def test_clawhub_metadata_is_text_only_self_contained():
    """ClawHub bundles must be text-only, so clawhub.json shouldn't reference
    screenshot/demo assets that aren't in the strict bundle (they're resolved
    from the homepage repo instead)."""
    data = json.loads((ROOT_DIR / "clawhub.json").read_text(encoding="utf-8"))
    assert "screenshots" not in data, "screenshots stay out of clawhub.json (homepage repo resolves them)"
    assert "demo" not in data, "demo gif stays out of clawhub.json (homepage repo resolves it)"


def test_release_bundle_contains_working_launchd_installer(tmp_path):
    """The strict text-only ClawHub release bundle must include the launchd
    installer code so `youos service install` works after installation. The
    plist is built programmatically by app/core/service.py:build_plist() (no
    `deploy/` directory dependency), so we just verify the file is present
    and structurally intact."""
    out_dir = tmp_path / "youos-release-test"
    script = ROOT_DIR / "scripts" / "prepare_clawhub_release.sh"
    result = subprocess.run(
        ["bash", str(script), str(out_dir)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"prepare_clawhub_release.sh failed:\n{result.stderr}"
    installer = out_dir / "app" / "core" / "service.py"
    assert installer.exists(), "release bundle missing app/core/service.py (launchd installer)"
    src = installer.read_text(encoding="utf-8")
    # Function present, generates plist programmatically (no deploy/ files needed),
    # talks to launchctl, and configures persistence + crash-restart.
    assert "def build_plist(" in src
    assert "launchctl" in src
    assert "<key>RunAtLoad</key>" in src
    assert "<key>KeepAlive</key>" in src
