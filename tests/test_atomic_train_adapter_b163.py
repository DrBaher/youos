"""b163: atomic LoRA adapter promotion + atomic train-corpus export writes."""

from __future__ import annotations

import os
import stat

from scripts.export_feedback_jsonl import _atomic_write_jsonl
from scripts.finetune_lora import _promote_adapter


def _valid_safetensors() -> bytes:
    """Minimal structurally-valid safetensors: 8-byte LE header length + JSON header."""
    header = b"{}"
    return len(header).to_bytes(8, "little") + header


_CORRUPT = b"\x05\x00\x00\x00\x00\x00\x00\x00tru"  # header claims 5 bytes, only 3 present


def test_promote_adapter_moves_valid_staged_adapter(tmp_path):
    staging = tmp_path / "latest.staging"
    staging.mkdir()
    (staging / "adapters.safetensors").write_bytes(_valid_safetensors())
    (staging / "adapter_config.json").write_text("{}")
    live = tmp_path / "latest"

    assert _promote_adapter(staging, live) is True
    assert (live / "adapters.safetensors").exists()
    assert (live / "adapter_config.json").exists()
    assert not (staging / "adapters.safetensors").exists()  # moved, not copied


def test_promote_adapter_refuses_corrupt_and_keeps_live(tmp_path):
    """A corrupt train must NOT clobber the previous good live adapter."""
    live = tmp_path / "latest"
    live.mkdir()
    (live / "adapters.safetensors").write_bytes(_valid_safetensors())  # previous good
    prev = (live / "adapters.safetensors").read_bytes()

    staging = tmp_path / "latest.staging"
    staging.mkdir()
    (staging / "adapters.safetensors").write_bytes(_CORRUPT)

    assert _promote_adapter(staging, live) is False
    assert (live / "adapters.safetensors").read_bytes() == prev  # untouched


def test_promote_adapter_false_when_no_staged_file(tmp_path):
    staging = tmp_path / "latest.staging"
    staging.mkdir()
    assert _promote_adapter(staging, tmp_path / "latest") is False


def test_atomic_write_jsonl_complete_and_no_residue(tmp_path):
    out = tmp_path / "train.jsonl"
    _atomic_write_jsonl(out, [{"a": 1}, {"b": 2}, {"c": 3}])
    lines = out.read_text().splitlines()
    assert len(lines) == 3
    assert not (tmp_path / "train.jsonl.tmp").exists()  # temp cleaned up by os.replace
    assert oct(stat.S_IMODE(os.stat(out).st_mode)) == "0o600"  # raw email bodies, owner-only
