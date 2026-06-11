"""b247: subprocess returncode hygiene — failures surface instead of
masquerading as success.

The headline: the wizard's /api/finetune chained export → finetune → eval
with check=False and DEVNULL, so a failed export still trained (on the stale
corpus) and /api/finetune/status said "done" because an adapter file existed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def test_finetune_chain_aborts_on_first_failure(tmp_path, monkeypatch):
    """Stage 1 failing must stop the chain and record an error status."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    # Fake stage scripts: export fails, finetune/benchmark record being run.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "export_feedback_jsonl.py").write_text("import sys; sys.exit(3)")
    (scripts / "finetune_lora.py").write_text(
        f"open({str(tmp_path / 'finetune_ran')!r}, 'w').write('x')"
    )
    (scripts / "run_golden_eval.py").write_text(
        f"open({str(tmp_path / 'eval_ran')!r}, 'w').write('x')"
    )

    import scripts.run_finetune_chain as chain_mod

    monkeypatch.setattr(chain_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(chain_mod, "get_var_dir", lambda: tmp_path / "var")
    rc = chain_mod.main()
    assert rc == 1
    assert not (tmp_path / "finetune_ran").exists()  # chain stopped at export
    assert not (tmp_path / "eval_ran").exists()
    status = json.loads((tmp_path / "var" / "finetune_status.json").read_text())
    assert status["status"] == "error"
    assert status["stage"] == "export"
    assert "exit 3" in status["detail"]


def test_finetune_chain_happy_path_records_done(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("export_feedback_jsonl.py", "finetune_lora.py", "run_golden_eval.py"):
        (scripts / name).write_text("print('ok')")

    import scripts.run_finetune_chain as chain_mod

    monkeypatch.setattr(chain_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(chain_mod, "get_var_dir", lambda: tmp_path / "var")
    assert chain_mod.main() == 0
    status = json.loads((tmp_path / "var" / "finetune_status.json").read_text())
    assert status["status"] == "done"
    log = (tmp_path / "var" / "finetune_chain.log").read_text()
    assert "export" in log and "finetune" in log and "benchmark" in log


def test_finetune_status_prefers_chain_record(monkeypatch, tmp_path):
    """An error recorded by the chain must not be masked into 'done' by a
    pre-existing adapter file."""
    import app.api.stats_routes as sr

    monkeypatch.setattr(sr, "_finetune_proc", None)
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_bytes(b"x")
    monkeypatch.setattr(sr, "get_adapter_path", lambda: adapter_dir)
    var = tmp_path / "var"
    var.mkdir()
    (var / "finetune_status.json").write_text(
        json.dumps({"status": "error", "stage": "finetune", "detail": "exit 1"})
    )
    import app.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_var_dir", lambda: var)
    body = client.get("/api/finetune/status").json()
    assert body["status"] == "error"
    assert body["stage"] == "finetune"
    assert body["adapter_ready"] is True  # the stale file no longer implies success


def test_youos_serve_propagates_uvicorn_exit_code():
    """`youos serve` source-level: the bind goes through _run (exit-code
    propagating), not bare subprocess.run."""
    src = (ROOT / "app" / "cli.py").read_text()
    serve_src = src.split("def serve():")[1].split("@app.command")[0]
    assert "_run(" in serve_src
    assert "subprocess.run(" not in serve_src


def test_wizard_and_teardown_check_cron_rc():
    wizard = (ROOT / "scripts" / "setup_wizard.py").read_text()
    assert "OpenClaw registration FAILED" in wizard
    assert "Database init FAILED" in wizard
    teardown = (ROOT / "scripts" / "teardown.py").read_text()
    assert "failed to remove the nightly cron job" in teardown


def test_stream_routes_drain_stderr_helper_bounds_and_captures():
    from app.api.stream_routes import _drain_stderr

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    buf = _drain_stderr(proc)
    proc.wait(timeout=10)
    import time

    for _ in range(50):  # drain thread is async; give it a beat
        if buf:
            break
        time.sleep(0.05)
    assert "boom" in "".join(buf)
