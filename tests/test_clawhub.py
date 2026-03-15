"""Tests for clawhub.json metadata."""
import json
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

    required = ["name", "version", "displayName", "description", "author",
                "homepage", "category", "tags", "requires", "emoji", "license"]
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
