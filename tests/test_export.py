"""Tests for youos export/import commands."""

import tarfile

import pytest


@pytest.fixture
def fake_project(tmp_path):
    """Create a minimal fake project structure."""
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "youos.db").write_text("fake-db")
    (tmp_path / "youos_config.yaml").write_text("user:\n  name: Test\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "persona.yaml").write_text("name: Test\n")
    adapters = tmp_path / "models" / "adapters" / "latest"
    adapters.mkdir(parents=True)
    (adapters / "adapters.safetensors").write_text("fake-weights")
    return tmp_path


def _create_archive(fake_project, archive_path):
    """Helper to create an archive from fake project."""
    include_paths = [
        ("var/youos.db", fake_project / "var" / "youos.db"),
        ("youos_config.yaml", fake_project / "youos_config.yaml"),
    ]
    configs_dir = fake_project / "configs"
    for f in configs_dir.rglob("*"):
        if f.is_file():
            include_paths.append((str(f.relative_to(fake_project)), f))
    adapters_dir = fake_project / "models" / "adapters" / "latest"
    for f in adapters_dir.rglob("*"):
        if f.is_file():
            include_paths.append((str(f.relative_to(fake_project)), f))

    with tarfile.open(archive_path, "w:gz") as tar:
        for arcname, filepath in include_paths:
            if filepath.exists():
                tar.add(str(filepath), arcname=arcname)
    return archive_path


def test_create_archive(fake_project, tmp_path):
    archive = tmp_path / "backup.tar.gz"
    _create_archive(fake_project, archive)
    assert archive.exists()
    assert archive.stat().st_size > 0


def test_archive_contents(fake_project, tmp_path):
    archive = tmp_path / "backup.tar.gz"
    _create_archive(fake_project, archive)

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "var/youos.db" in names
    assert "youos_config.yaml" in names
    assert "configs/persona.yaml" in names
    assert "models/adapters/latest/adapters.safetensors" in names


def test_import_roundtrip(fake_project, tmp_path):
    archive = tmp_path / "backup.tar.gz"
    _create_archive(fake_project, archive)

    # Extract to a new directory
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(path=restore_dir, filter="data")

    assert (restore_dir / "var" / "youos.db").read_text() == "fake-db"
    assert (restore_dir / "youos_config.yaml").read_text() == "user:\n  name: Test\n"
    assert (restore_dir / "configs" / "persona.yaml").exists()
    assert (restore_dir / "models" / "adapters" / "latest" / "adapters.safetensors").exists()


def test_archive_excludes_unwanted(fake_project, tmp_path):
    """Verify .venv, __pycache__, and data/ are not included."""
    # Create dirs that should be excluded
    (fake_project / ".venv").mkdir()
    (fake_project / ".venv" / "lib.py").write_text("x")
    (fake_project / "__pycache__").mkdir()
    (fake_project / "__pycache__" / "mod.pyc").write_bytes(b"\x00")
    (fake_project / "data").mkdir()
    (fake_project / "data" / "big.bin").write_bytes(b"\x00" * 100)

    archive = tmp_path / "backup.tar.gz"
    _create_archive(fake_project, archive)

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    for name in names:
        assert not name.startswith(".venv")
        assert "__pycache__" not in name
        assert not name.startswith("data/")
