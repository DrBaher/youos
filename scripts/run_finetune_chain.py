"""Wizard-triggered finetune chain: export → finetune → golden eval (b247).

The old /api/finetune handler chained these with `check=False` and DEVNULL —
a failed export didn't stop the finetune (which then trained on the previous
stale train.jsonl), the golden eval "validated" it, nothing was logged
anywhere, and /api/finetune/status reported "done" because it only checked
that an adapter file exists.

This runner aborts on the first failed stage, bounds each stage with a
timeout, appends all child output to var/finetune_chain.log, and records
progress/outcome in var/finetune_status.json for /api/finetune/status.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.atomic_io import atomic_write_json  # noqa: E402
from app.core.settings import get_var_dir  # noqa: E402

# (stage, script, timeout_seconds). Generous timeouts — these bound a HUNG
# child (the old chain had none, wedging the 409 guard until server restart),
# not a slow-but-working one.
STAGES = [
    ("export", "export_feedback_jsonl.py", 1800),
    ("finetune", "finetune_lora.py", 14400),
    ("benchmark", "run_golden_eval.py", 7200),
]

STATUS_FILE = "finetune_status.json"
LOG_FILE = "finetune_chain.log"


def _write_status(status: str, stage: str, detail: str = "") -> None:
    try:
        atomic_write_json(
            get_var_dir() / STATUS_FILE,
            {
                "status": status,  # running | done | error
                "stage": stage,
                "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except OSError:
        pass  # status reporting must never break the chain itself


def main() -> int:
    log_path = get_var_dir() / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log:
        for stage, script, timeout in STAGES:
            _write_status("running", stage)
            log.write(f"\n===== {stage}: {script} =====\n".encode())
            log.flush()
            try:
                rc = subprocess.run(  # noqa: S603
                    [sys.executable, str(ROOT_DIR / "scripts" / script)],
                    cwd=str(ROOT_DIR),
                    stdout=log,
                    stderr=log,
                    timeout=timeout,
                ).returncode
            except subprocess.TimeoutExpired:
                _write_status("error", stage, f"timed out after {timeout}s — see var/{LOG_FILE}")
                return 1
            if rc != 0:
                _write_status("error", stage, f"exit {rc} — see var/{LOG_FILE}")
                return 1
    _write_status("done", "complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
