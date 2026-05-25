"""Run-the-server-reliably launchd service (youos service).

Pins the LaunchAgent plist generation and the install/uninstall/status logic.
launchctl + the real LaunchAgents path are mocked so tests never install a
real agent.
"""

from __future__ import annotations

import types
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app
from app.core import service

runner = CliRunner()


# --- plist generation ------------------------------------------------------


def test_plist_has_server_args_and_keepalive():
    p = service.build_plist(
        python="/venv/bin/python", root=Path("/repo"), host="127.0.0.1", port=8901,
        log_path=Path("/repo/var/server.log"), data_dir=None,
    )
    assert "<string>com.youos.server</string>" in p
    assert "uvicorn" in p and "app.main:app" in p
    assert "<string>127.0.0.1</string>" in p and "<string>8901</string>" in p
    assert "<key>RunAtLoad</key><true/>" in p and "<key>KeepAlive</key><true/>" in p
    assert "/venv/bin/python" in p
    assert "/repo/var/server.log" in p


def test_plist_includes_data_dir_when_set():
    p = service.build_plist(python="/p", root=Path("/r"), host="h", port=1, log_path=Path("/l"), data_dir="/data/inst")
    assert "YOUOS_DATA_DIR" in p and "/data/inst" in p


def test_plist_omits_env_block_when_no_data_dir():
    p = service.build_plist(python="/p", root=Path("/r"), host="h", port=1, log_path=Path("/l"), data_dir=None)
    assert "YOUOS_DATA_DIR" not in p


def test_plist_path_is_user_launchagent():
    assert service.plist_path() == Path.home() / "Library" / "LaunchAgents" / "com.youos.server.plist"


# --- install / uninstall / status ------------------------------------------


def _fake_run(rc=0):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")

    run.calls = calls
    return run


def test_install_writes_plist_and_loads(monkeypatch, tmp_path):
    target = tmp_path / "com.youos.server.plist"
    monkeypatch.setattr(service, "plist_path", lambda: target)
    run = _fake_run(rc=0)
    monkeypatch.setattr(service.subprocess, "run", run)
    ok, _ = service.install()
    assert ok is True
    assert target.exists() and "app.main:app" in target.read_text()
    assert any("load" in a for a in run.calls)  # launchctl load invoked


def test_install_reports_launchctl_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "x.plist")
    monkeypatch.setattr(service.subprocess, "run", _fake_run(rc=1))
    ok, msg = service.install()
    assert ok is False and "launchctl" in msg


def test_uninstall_removes_plist(monkeypatch, tmp_path):
    target = tmp_path / "x.plist"
    target.write_text("x")
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service.subprocess, "run", _fake_run(rc=0))
    ok, _ = service.uninstall()
    assert ok is True and not target.exists()


def test_status_states(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "absent.plist")
    monkeypatch.setattr(service, "is_loaded", lambda: False)
    assert service.status() == "not installed"
    p = tmp_path / "present.plist"
    p.write_text("x")
    monkeypatch.setattr(service, "plist_path", lambda: p)
    assert service.status() == "installed but not loaded"
    monkeypatch.setattr(service, "is_loaded", lambda: True)
    assert service.status() == "running (LaunchAgent loaded)"


# --- CLI -------------------------------------------------------------------


def test_cli_service_status(monkeypatch):
    monkeypatch.setattr(service, "status", lambda: "not installed")
    r = runner.invoke(app, ["service", "status"])
    assert r.exit_code == 0 and "not installed" in r.stdout


def test_cli_service_install_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(service, "install", lambda: (False, "launchctl boom"))
    r = runner.invoke(app, ["service", "install"])
    assert r.exit_code == 1
