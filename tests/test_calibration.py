"""Tests for needs-reply score calibration (Phase A2).

Calibration maps the raw additive score to an empirical P(deserved a reply),
learned from the user's own verdicts. Dormant until there's enough data.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent.calibration import (
    Calibrator,
    _pav,
    fit,
    fit_from_database,
    load_calibrator,
    save_calibrator,
)
from app.db.bootstrap import _migrate_agent_pending_drafts

# --- isotonic regression (PAV) ---------------------------------------------


def test_pav_already_monotonic_unchanged():
    out = _pav([0.1, 0.3, 0.6, 0.9], [1, 1, 1, 1])
    assert out == [0.1, 0.3, 0.6, 0.9]


def test_pav_pools_violations_into_monotonic():
    # A dip (0.5 then 0.2) must be pooled so the result is non-decreasing.
    out = _pav([0.5, 0.2], [1, 1])
    assert out == [pytest.approx(0.35), pytest.approx(0.35)]
    assert all(out[i] <= out[i + 1] + 1e-9 for i in range(len(out) - 1))


def test_pav_weighted_mean():
    out = _pav([0.8, 0.0], [3, 1])  # weighted: (0.8*3 + 0*1)/4 = 0.6
    assert out[0] == pytest.approx(0.6)
    assert out[1] == pytest.approx(0.6)


# --- fit -------------------------------------------------------------------


def test_fit_returns_none_below_min_samples():
    samples = [(0.5, 1), (0.6, 0)]
    assert fit(samples, min_samples=50) is None


def test_fit_produces_monotonic_calibrator():
    # Low scores mostly negative, high scores mostly positive.
    samples = []
    for _ in range(40):
        samples.append((0.2, 0))
    for _ in range(40):
        samples.append((0.8, 1))
    cal = fit(samples, min_samples=10, bins=10)
    assert cal is not None
    # Calibrated probability should rise with score.
    assert cal.probability(0.2) < cal.probability(0.8)
    # Bins with all-negatives/all-positives are smoothed off the extremes.
    assert 0.0 < cal.probability(0.2) < 0.5
    assert 0.5 < cal.probability(0.8) < 1.0


def test_calibrator_interpolates_and_clamps():
    cal = Calibrator(centers=[0.25, 0.75], probs=[0.2, 0.8], n_samples=100)
    assert cal.probability(0.0) == 0.2          # clamp low
    assert cal.probability(1.0) == 0.8          # clamp high
    assert cal.probability(0.5) == pytest.approx(0.5)  # midpoint interpolation


def test_calibrator_roundtrip(tmp_path):
    cal = Calibrator(centers=[0.25, 0.75], probs=[0.3, 0.9], n_samples=120)
    p = tmp_path / "cal.json"
    save_calibrator(cal, path=p)
    loaded = load_calibrator(path=p)
    assert loaded is not None
    assert loaded.centers == cal.centers
    assert loaded.probs == cal.probs
    assert loaded.n_samples == 120


def test_load_calibrator_missing_returns_none(tmp_path):
    assert load_calibrator(path=tmp_path / "nope.json") is None


# --- fit_from_database -----------------------------------------------------


def test_fit_from_database_uses_verdicts(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    n = [0]

    def insert(score, status, reason=None):
        n[0] += 1
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, needs_reply_score, reasons_json, "
            " cold_outreach, tier, status, dismissal_reason) "
            "VALUES (?, ?, 'a@x.com', ?, '[]', 0, 'draft', ?, ?)",
            (f"m{n[0]}", f"t{n[0]}", score, status, reason),
        )

    # 30 high-score positives, 30 low-score negatives.
    for _ in range(30):
        insert(0.9, "sent")
    for _ in range(30):
        insert(0.3, "dismissed", "noise")
    conn.commit()
    conn.close()

    cal = fit_from_database(f"sqlite:///{db}", days=365, min_samples=10)
    assert cal is not None
    assert cal.n_samples == 60
    assert cal.probability(0.3) < cal.probability(0.9)


def test_fit_from_database_counts_replied_anywhere_as_positive(tmp_path):
    """b271 signal: a queued row the user actually replied to (reply_pairs
    inbound_message_ids) is positive even if still 'pending' in YouOS. Without it
    these are excluded and the calibrator learns a falsely-low P."""
    import json

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, metadata_json TEXT)")

    def insert(score, status, mid, reason=None):
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, needs_reply_score, reasons_json, "
            " cold_outreach, tier, status, dismissal_reason) "
            "VALUES (?, ?, 'a@x.com', ?, '[]', 0, 'draft', ?, ?)",
            (mid, f"t{mid}", score, status, reason),
        )

    # 30 pending high-score rows the user actually replied to (outside YouOS) +
    # 30 low-score noise dismissals. Without the reply signal the 30 pending rows
    # are excluded → only negatives remain → degenerate. With it they're positive.
    for i in range(30):
        insert(0.8, "pending", f"hi{i}")
        conn.execute(
            "INSERT INTO reply_pairs (metadata_json) VALUES (?)",
            (json.dumps({"inbound_message_ids": [f"hi{i}"]}),),
        )
    for i in range(30):
        insert(0.3, "dismissed", f"lo{i}", "noise")
    conn.commit()
    conn.close()

    cal = fit_from_database(f"sqlite:///{db}", days=365, min_samples=10)
    assert cal is not None
    assert cal.n_samples == 60  # the 30 replied 'pending' rows are now labelable
    # The replied high-score rows pull P(0.8) well above the noise P(0.3).
    assert cal.probability(0.8) > 0.5
    assert cal.probability(0.8) > cal.probability(0.3)


def test_fit_from_database_none_when_no_decided_rows(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, needs_reply_score, reasons_json, "
        " cold_outreach, tier, status) "
        "VALUES ('m1','t1','a@x.com',0.7,'[]',0,'draft','pending')"
    )
    conn.commit()
    conn.close()
    assert fit_from_database(f"sqlite:///{db}", days=365, min_samples=10) is None
