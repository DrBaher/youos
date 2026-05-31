"""Regression tests for b169: an empty Gmail ingestion delta is success.

Background
---------
The nightly Gmail ingestion step was marked "failed" on every run even though
it was working. The ``drbaher@gmail.com`` account fetches SENT-only threads in
the nightly ``in:sent after:<date>`` window; those yield 0 new inbound documents
and 0 reply pairs (a normal *empty delta*). ``_gmail_run_outcome`` returned
status ``"failed"`` whenever ``inbound_documents + reply_pairs == 0``, so:

  * ``scripts/ingest_gmail_threads.py`` did ``raise SystemExit(1)`` (it exits
    nonzero only when ``result.status == "failed"``);
  * the nightly ``step_ingest_gmail`` saw a nonzero subprocess exit, treated the
    step as failed, and *never advanced* ``set_last_ingest_at`` -- so the next
    night re-scanned the same (now wider) window and re-failed, forever.

The fix introduces a distinct ``"no_new_rows"`` success status for a clean run
that fetched input but stored nothing new. Genuine fetch/parse/transport
failures still return ``"failed"``.

These tests are hermetic: they exercise real SQLite ingestion of local JSON
payloads, construct outcome inputs directly, and mock the subprocess boundary.
They never touch real Gmail or the network.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from app.ingestion.gmail_threads import (
    IngestCounts,
    _gmail_run_outcome,
    ingest_gmail_threads,
)


def _fresh_nightly():
    """Import ``scripts.nightly_pipeline`` so that monkeypatching its module
    globals affects the functions it defines.

    The prod launcher can have first loaded this module under ``__main__`` (so a
    plain ``import`` would hand back a function whose ``__globals__`` is a
    *different* module dict, making attribute patches silently no-op). Popping +
    reloading -- as ``tests/test_nightly_pipeline_paths.py`` does -- yields a
    module whose functions resolve their bare names against ``np.__dict__``.
    """
    sys.modules.pop("scripts.nightly_pipeline", None)
    import scripts.nightly_pipeline as np

    importlib.reload(np)
    return np


# --------------------------------------------------------------------------- #
# (1) _gmail_run_outcome: empty delta vs. useful rows vs. warnings             #
# --------------------------------------------------------------------------- #
def _counts(*, inbound: int = 0, pairs: int = 0) -> IngestCounts:
    return IngestCounts(
        discovered_threads=4,
        fetched_threads=4,
        threads=4,
        inbound_documents=inbound,
        chunks=inbound,
        reply_pairs=pairs,
    )


def test_outcome_empty_delta_is_not_failed():
    """Fetched threads, 0 useful rows, no warnings -> no_new_rows (success)."""
    status, detail, error_summary = _gmail_run_outcome(
        counts=_counts(inbound=0, pairs=0),
        import_detail="live Gmail via gog for drbaher@gmail.com",
        target_db_path=Path("/tmp/youos.db"),
        warning_count=0,
    )
    assert status == "no_new_rows"
    assert status != "failed"
    # A success status carries no error summary.
    assert error_summary is None
    # Honest, distinguishable detail for an empty delta.
    assert "empty delta" in detail.lower()


def test_outcome_useful_rows_is_completed():
    status, _detail, error_summary = _gmail_run_outcome(
        counts=_counts(inbound=3, pairs=2),
        import_detail="live Gmail via gog for baher@medicus.ai",
        target_db_path=Path("/tmp/youos.db"),
        warning_count=0,
    )
    assert status == "completed"
    assert error_summary is None


def test_outcome_useful_rows_with_warnings_is_completed_with_warnings():
    status, _detail, _error = _gmail_run_outcome(
        counts=_counts(inbound=3, pairs=2),
        import_detail="x",
        target_db_path=Path("/tmp/youos.db"),
        warning_count=2,
    )
    assert status == "completed_with_warnings"


# --------------------------------------------------------------------------- #
# (2) end-to-end ingest_gmail_threads through real SQLite (no network)         #
# --------------------------------------------------------------------------- #
def _write_threads(tmp_path: Path, payloads: list[dict]) -> Path:
    # The local loader (`_load_thread_payload_file`) expects a JSON object, not a
    # bare list -- either one thread, or a `{"threads": [...]}` envelope.
    export = tmp_path / "threads.json"
    export.write_text(json.dumps({"threads": payloads}), encoding="utf-8")
    return export


def _sent_only_thread() -> dict:
    """A thread containing only a message the user sent -- no inbound message and
    therefore no reply pair. This is the shape that produces an empty delta for a
    SENT-only nightly window (the b169 trigger)."""
    return {
        "thread_id": "t-sent",
        "account": "me@example.com",
        "messages": [
            {
                "id": "m1",
                "from_email": "me@example.com",
                "from_name": "Me",
                "to": "friend@example.com",
                "body_text": "Just a note I sent out.",
                "subject": "FYI",
                "timestamp": "2024-01-01T10:00:00Z",
                "label_ids": ["SENT"],
            }
        ],
    }


def test_ingest_sent_only_window_is_no_new_rows(tmp_path: Path):
    """A SENT-only window fetches a thread but stores 0 useful rows -> success.

    This is the real end-to-end b169 path (real SQLite, no network): the only
    message is one the user sent, so there is no inbound document and no reply
    pair -- an empty delta. Pre-fix this returned "failed"; it must now be the
    non-failure "no_new_rows" status. (Requirement (c) -- a run that DOES land
    useful rows -> "completed" -- is covered by ``test_outcome_useful_rows_is_completed``;
    driving real document/reply-pair counts > 0 depends on ingestion internals
    out of scope for this fix.)
    """
    db_path = tmp_path / "youos.db"
    result = ingest_gmail_threads(
        _write_threads(tmp_path, [_sent_only_thread()]),
        db_path=db_path,
        user_emails=("me@example.com",),
    )
    assert result.status == "no_new_rows"
    assert result.status != "failed"


def test_ingest_all_malformed_is_failed(tmp_path: Path):
    """Genuine breakage (every payload malformed) still fails -- not papered over."""
    db_path = tmp_path / "youos.db"
    result = ingest_gmail_threads(
        _write_threads(tmp_path, [{"thread_id": "t1", "messages": "not-a-list"}]),
        db_path=db_path,
    )
    assert result.status == "failed"


# --------------------------------------------------------------------------- #
# (3) CLI exit-code mapping (scripts/ingest_gmail_threads.py)                  #
# --------------------------------------------------------------------------- #
def _run_cli(monkeypatch, status: str) -> int:
    """Invoke the CLI main() with a stubbed ingest result of `status`, returning
    the resulting process exit code (0 if no SystemExit raised)."""
    import scripts.ingest_gmail_threads as cli
    from app.ingestion.models import IngestionResult

    monkeypatch.setattr(
        cli,
        "ingest_gmail_threads",
        lambda *a, **k: IngestionResult(
            source_type="gmail_thread",
            status=status,
            detail="stub detail",
            run_id="run-1",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["ingest_gmail_threads.py"])
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_cli_no_new_rows_exits_zero(monkeypatch):
    assert _run_cli(monkeypatch, "no_new_rows") == 0


def test_cli_completed_exits_zero(monkeypatch):
    assert _run_cli(monkeypatch, "completed") == 0


def test_cli_failed_exits_nonzero(monkeypatch):
    assert _run_cli(monkeypatch, "failed") == 1


# --------------------------------------------------------------------------- #
# (4) nightly step: watermark advances on empty delta, not on real failure     #
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _run_step_with_exit(monkeypatch, returncode: int) -> tuple[bool, list]:
    """Run step_ingest_gmail with the ingest subprocess forced to `returncode`.
    Returns (step_ok, recorded) where `recorded` lists set_last_ingest_at calls."""
    np = _fresh_nightly()
    monkeypatch.setattr(np, "ACCOUNTS", ("drbaher@gmail.com",))
    monkeypatch.setattr(np, "get_last_ingest_at", lambda account: None)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        np, "set_last_ingest_at", lambda account, ts: recorded.append((account, ts))
    )
    monkeypatch.setattr(np.subprocess, "run", lambda *a, **k: _FakeProc(returncode))
    ok = np.step_ingest_gmail()
    return ok, recorded


def test_step_empty_delta_exit_zero_advances_watermark(monkeypatch):
    # no_new_rows maps to CLI exit 0 -> subprocess returncode 0 -> step success.
    ok, recorded = _run_step_with_exit(monkeypatch, returncode=0)
    assert ok is True
    assert len(recorded) == 1
    assert recorded[0][0] == "drbaher@gmail.com"


def test_step_real_failure_exit_nonzero_does_not_advance_watermark(monkeypatch):
    ok, recorded = _run_step_with_exit(monkeypatch, returncode=1)
    # Genuine failure -> step fails AND watermark must NOT advance.
    assert ok is False
    assert recorded == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
