"""b250: the nightly's overall status distinguishes a clean run from one
where "never fails the run" observability steps recorded errors.

Before: steps like queue_feedback/threshold_tuner set steps[name]=True in
their except blocks, so pipeline_last_run.json said status "ok" while
results held "error: ..." — invisible unless someone read the results blob.
"""

from __future__ import annotations

from scripts.nightly_pipeline import _derive_status


def test_clean_run_is_ok():
    status, warns = _derive_status(
        {"ingestion": True, "queue_feedback": True},
        {"ingestion": {"n": 5}, "queue_feedback": {"captured": 2}},
    )
    assert (status, warns) == ("ok", [])


def test_swallowed_step_error_yields_ok_with_warnings():
    status, warns = _derive_status(
        {"ingestion": True, "queue_feedback": True, "threshold_tuner": True},
        {
            "ingestion": {"n": 5},
            "queue_feedback": "error: db locked",
            "threshold_tuner": "error: no outcomes",
        },
    )
    assert status == "ok_with_warnings"
    assert warns == ["queue_feedback", "threshold_tuner"]


def test_hard_failure_still_partial_or_failed():
    status, warns = _derive_status(
        {"ingestion": False, "queue_feedback": True},
        {"ingestion": "error: boom", "queue_feedback": "error: also"},
    )
    assert status == "partial"
    assert warns == ["queue_feedback"]  # only steps that CLAIMED success
    status, _ = _derive_status({"a": False, "b": False}, {})
    assert status == "failed"


def test_non_error_string_results_do_not_warn():
    status, warns = _derive_status(
        {"recent_commits": True},
        {"recent_commits": "abc123 some commit"},
    )
    assert (status, warns) == ("ok", [])
