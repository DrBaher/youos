"""Triage precision/recall harness."""

from __future__ import annotations

import json
from pathlib import Path

from app.evaluation.triage_eval import best_threshold, evaluate_triage, threshold_sweep

FIXTURE = Path(__file__).resolve().parents[1] / "configs" / "triage_corpus.jsonl"


def _load():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_fixture_classifies_well_and_confusion_sums_to_n():
    cases = _load()
    res = evaluate_triage(cases, threshold=0.6)
    c = res["confusion"]
    assert c["tp"] + c["fp"] + c["tn"] + c["fn"] == res["n"] == len(cases)
    # The fixture is clean-cut, so the classifier should score it highly.
    assert res["f1"] >= 0.8
    assert 0.0 <= res["precision"] <= 1.0
    assert 0.0 <= res["recall"] <= 1.0


def test_confusion_and_errors_count_misclassifications():
    # A newsletter mislabelled as needs_reply → the classifier predicts False
    # (hard-skip) so this is a false negative against the (wrong) label.
    cases = [
        {"label": True, "sender": "x", "sender_email": "x@x.com", "subject": "n",
         "body": "noise", "headers": {"list-unsubscribe": "<mailto:u@x.com>"}},
        {"label": True, "sender": "Real <r@x.com>", "sender_email": "r@x.com",
         "subject": "q", "body": "Could you confirm the budget? Thanks."},
    ]
    res = evaluate_triage(cases, threshold=0.6)
    assert res["confusion"]["fn"] == 1  # the hard-skipped newsletter
    assert res["confusion"]["tp"] == 1  # the real question
    assert any(e["kind"] == "false_negative" for e in res["errors"])


def test_threshold_sweep_and_best_threshold():
    cases = _load()
    sweep = threshold_sweep(cases, thresholds=(0.5, 0.6, 0.7, 0.8))
    assert [r["threshold"] for r in sweep] == [0.5, 0.6, 0.7, 0.8]
    assert all("f1" in r for r in sweep)
    bt = best_threshold(cases, thresholds=(0.5, 0.6, 0.7, 0.8))
    assert bt in (0.5, 0.6, 0.7, 0.8)
