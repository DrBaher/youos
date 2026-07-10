"""b295: the daemon runs with a minimal PATH that excludes ~/.local/bin (the
native claude install location), so a bare "claude" subprocess fails with
FileNotFoundError and silently collapses every cloud call to the fallback.
_call_claude_cli must resolve the binary to an absolute path regardless of PATH.
"""
from __future__ import annotations

import app.generation.service as svc


def test_resolve_prefers_path(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda name: "/somewhere/on/path/claude")
    assert svc._resolve_claude_bin() == "/somewhere/on/path/claude"


def test_resolve_falls_back_to_local_bin_when_not_on_path(monkeypatch, tmp_path):
    # PATH lookup fails (the daemon case) → probe known install locations.
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    fake = tmp_path / ".local" / "bin" / "claude"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(svc.Path, "home", classmethod(lambda cls: tmp_path))
    assert svc._resolve_claude_bin() == str(fake)


def test_resolve_returns_bare_name_when_nothing_found(monkeypatch, tmp_path):
    # Nothing resolvable → bare name so the subprocess raises a clear error
    # rather than silently doing nothing.
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    monkeypatch.setattr(svc.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(svc.Path, "exists", lambda self: False)
    assert svc._resolve_claude_bin() == "claude"


def test_call_claude_cli_uses_resolved_bin(monkeypatch):
    monkeypatch.setattr(svc, "_resolve_claude_bin", lambda: "/abs/claude")
    seen = {}

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, *, timeout=None):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(svc, "_run_subprocess", fake_run)
    svc._call_claude_cli("hi", model="sonnet")
    assert seen["cmd"][0] == "/abs/claude"
    assert seen["cmd"][:4] == ["/abs/claude", "--print", "--model", "sonnet"]
