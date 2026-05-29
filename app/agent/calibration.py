"""Calibrate the raw needs-reply score to an empirical probability.

The classifier's score is an additive heuristic: 0.85 does **not** mean "85%
likely to deserve a reply." Calibration fixes that — it learns, from the user's
own verdicts on past queue rows, what fraction of messages at each score level
actually deserved a reply, and maps a raw score to that empirical probability.

A calibrated probability is what an *act* decision (auto-push / later auto-send)
should gate on, because it can be tied to a real precision target ("only act
when P(deserved) ≥ 0.9") instead of a meaningless raw cutoff.

Method: bin labeled (score, outcome) pairs, Laplace-smooth each bin's positive
rate, then enforce monotonicity with pool-adjacent-violators (isotonic
regression). Deterministic, dependency-free. **Dormant until there's data** —
``fit`` returns ``None`` below ``min_samples`` (the current state: a fresh
instance has no decided rows), and callers simply skip calibration, leaving the
raw heuristic in charge. It self-activates as real verdicts accumulate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MIN_SAMPLES = 50
DEFAULT_BINS = 10
_ALPHA = 1.0  # Laplace smoothing — a 1-sample bin isn't pinned to 0.0 or 1.0


def _pav(means: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators isotonic regression. Returns a non-decreasing
    sequence the same length as ``means``, each the weighted mean of its
    merged block."""
    stack: list[list[float]] = []  # [mean, weight, n_bins]
    for m, w in zip(means, weights, strict=False):
        block = [m, w, 1.0]
        while stack and stack[-1][0] >= block[0]:
            pm, pw, pn = stack.pop()
            tw = pw + block[1]
            wm = (pm * pw + block[0] * block[1]) / tw if tw else block[0]
            block = [wm, tw, pn + block[2]]
        stack.append(block)
    out: list[float] = []
    for m, _w, n in stack:
        out.extend([m] * int(n))
    return out


@dataclass
class Calibrator:
    """A monotonic score → probability map, stored as (center, prob) knots.

    ``probability`` linearly interpolates between knot centers and clamps to
    the end knots outside the observed range."""

    centers: list[float]
    probs: list[float]
    n_samples: int

    def probability(self, score: float) -> float:
        c, p = self.centers, self.probs
        if not c:
            return max(0.0, min(1.0, score))
        if score <= c[0]:
            return p[0]
        if score >= c[-1]:
            return p[-1]
        for i in range(1, len(c)):
            if score <= c[i]:
                lo_c, hi_c = c[i - 1], c[i]
                lo_p, hi_p = p[i - 1], p[i]
                if hi_c == lo_c:
                    return hi_p
                frac = (score - lo_c) / (hi_c - lo_c)
                return lo_p + frac * (hi_p - lo_p)
        return p[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "centers": [round(x, 6) for x in self.centers],
            "probs": [round(x, 6) for x in self.probs],
            "n_samples": self.n_samples,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibrator":
        return cls(
            centers=[float(x) for x in d.get("centers", [])],
            probs=[float(x) for x in d.get("probs", [])],
            n_samples=int(d.get("n_samples", 0)),
        )


def fit(
    samples: list[tuple[float, int]],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    bins: int = DEFAULT_BINS,
) -> Calibrator | None:
    """Fit a calibrator from ``(raw_score, label)`` pairs (label 0/1).

    Returns ``None`` when there are fewer than ``min_samples`` — the caller
    keeps the raw heuristic. Bins over [0, 1], drops empty bins, Laplace-
    smooths each bin's positive rate, then isotonic-regresses for monotonicity.
    """
    clean = [(float(s), 1 if y else 0) for s, y in samples if s is not None]
    if len(clean) < min_samples:
        return None

    width = 1.0 / bins
    pos = [0.0] * bins
    cnt = [0.0] * bins
    for s, y in clean:
        idx = min(bins - 1, max(0, int(s / width)))
        cnt[idx] += 1.0
        pos[idx] += y

    centers: list[float] = []
    means: list[float] = []
    weights: list[float] = []
    for i in range(bins):
        if cnt[i] == 0:
            continue
        centers.append((i + 0.5) * width)
        means.append((pos[i] + _ALPHA) / (cnt[i] + 2 * _ALPHA))
        weights.append(cnt[i])

    if not centers:
        return None
    calibrated = _pav(means, weights)
    calibrated = [max(0.0, min(1.0, p)) for p in calibrated]
    return Calibrator(centers=centers, probs=calibrated, n_samples=len(clean))


def fit_from_database(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 90,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    bins: int = DEFAULT_BINS,
) -> Calibrator | None:
    """Fit from decided queue rows: feature = the row's needs-reply score,
    label from the same truth-mapping the precision harness uses (deserved a
    reply = 1, didn't = 0; ambiguous rows are excluded)."""
    from contextlib import closing

    from app.agent.store import _connect
    from app.evaluation.real_mail_eval import _truth_label

    where = "date(created_at) >= date('now', ?)"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"SELECT needs_reply_score, status, dismissal_reason "
            f"FROM agent_pending_drafts WHERE {where}",
            params,
        ).fetchall()

    samples: list[tuple[float, int]] = []
    for r in rows:
        truth = _truth_label(r["status"], r["dismissal_reason"])
        if truth is None or r["needs_reply_score"] is None:
            continue
        samples.append((float(r["needs_reply_score"]), 1 if truth == "pos" else 0))
    return fit(samples, min_samples=min_samples, bins=bins)


# --- Persistence (a small JSON file in the instance var dir) ----------------


def _default_path():
    from app.core.settings import get_var_dir

    return get_var_dir() / "triage_calibrator.json"


def save_calibrator(cal: Calibrator, *, path=None) -> None:
    import json

    p = path or _default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cal.to_dict(), indent=2), encoding="utf-8")


def load_calibrator(*, path=None) -> Calibrator | None:
    """Load the persisted calibrator, or ``None`` if absent/unreadable."""
    import json

    p = path or _default_path()
    try:
        if not p.exists():
            return None
        return Calibrator.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None
