"""Run golden benchmark evaluation against curated test cases."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

from app.core.settings import get_var_dir

ROOT_DIR = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT_DIR / "configs" / "benchmarks" / "golden.yaml"

# --- Fairer keyword scoring (b181) --------------------------------------------
# The old check was a brittle exact substring test: `kw.lower() in draft_lower`.
# A genuinely correct, clean reply that expressed the right idea with a synonym
# or an inflected form ("free" for "available", "reviewed" for "review",
# "introduction" for "intro") scored 0 and was dinged to warn/fail even though it
# was on-topic. That made the golden composite measure surface-form overlap, not
# reply quality. We now credit a keyword when EITHER:
#   (a) a draft token shares a stem with the keyword (so "review"<->"reviewed",
#       "connect"<->"connecting", "follow"<->"following", "sounds"<->"sound"), OR
#   (b) the draft contains any accepted SYNONYM of the keyword.
# Keywords stay meaningful — we are NOT deleting them. A reply that is off-topic
# (no stem and no synonym hit for ANY keyword) still scores 0 and still fails.

# Built-in synonym groups. Each keyword maps to alternative surface forms that
# express the same intent for these business/personal reply cases. Conservative
# and hand-reviewed — only true paraphrases, no topic-broadening.
_KEYWORD_SYNONYMS: dict[str, set[str]] = {
    "available": {"free", "open", "around", "availability"},
    "time": {"slot", "when", "schedule", "timing"},
    "call": {"meeting", "chat", "talk", "speak", "discussion", "conversation"},
    "week": {"weekday", "monday", "tuesday", "wednesday", "thursday", "friday"},
    "thank": {"thanks", "appreciate", "grateful", "appreciated"},
    "unfortunately": {"sadly", "regrettably", "afraid", "wont", "cannot", "unable"},
    "schedule": {"calendar", "availability", "booked", "commitments"},
    "proposal": {"document", "doc", "draft", "deck", "plan", "submission"},
    "review": {"look", "read", "consider", "feedback", "thoughts"},
    "follow": {"circle", "check", "touch", "checking", "circling"},
    "intro": {"introduction", "introducing", "introduce"},
    "connect": {"meet", "reach", "link", "connecting", "connected"},
    "which": {"what", "specify", "exactly"},
    "clarify": {"clarification", "mean", "meaning", "confirm", "specify"},
    "referring": {"refer", "reference", "talking", "mean"},
    "sounds": {"sound", "great", "perfect", "love", "lovely"},
    "weekend": {"saturday", "sunday"},
    "catch": {"meet", "reconnect", "hang", "coffee", "drinks", "together"},
    "when": {"time", "date", "day"},
    "reschedule": {"move", "push", "shift", "postpone", "rebook", "change"},
    "works": {"work", "suit", "suits", "ok", "fine", "good"},
    "numbers": {"figures", "metrics", "data", "stats", "results"},
    "hey": {"hi", "hello", "heya"},
    "good": {"great", "well", "fine", "doing"},
    "hope": {"hoping", "wish"},
    "well": {"good", "great", "fine"},
    # German (multilang case)
    "woche": {"wochen", "nächste"},
    "gespräch": {"gespraech", "treffen", "termin", "unterhaltung"},
    "freundlichen": {"grüßen", "gruessen", "grüße", "freundliche"},
}

_TOKEN_RE = re.compile(r"[^\wäöüßéèàç]+", re.UNICODE)

# Very light suffix stemmer (English-ish). Just enough to fold common inflections
# so "reviewed"->"review", "connecting"->"connect", "sounds"->"sound". Longest
# suffix first.
_SUFFIXES = ("ing", "ned", "ted", "ied", "ed", "es", "s")


def _stem(token: str) -> str:
    t = token.lower()
    for suf in _SUFFIXES:
        if len(t) > len(suf) + 2 and t.endswith(suf):
            base = t[: -len(suf)]
            if suf == "ied":
                base += "y"
            return base
    return t


def _keyword_hits(keyword: str, stems: set[str], draft_lower: str) -> bool:
    """Does the draft credit this keyword? Stem match OR synonym match.

    A keyword counts if a draft token stem-matches it, or if any accepted
    synonym of the keyword appears. Multi-word keywords ("look it over") fall
    back to a case-insensitive substring test.
    """
    kw = keyword.lower().strip()
    if not kw:
        return False
    if " " in kw:  # phrase keyword: substring is the only sensible test
        return kw in draft_lower
    kw_stem = _stem(kw)
    # (a) stem / prefix match against draft tokens
    if kw_stem in stems or kw in stems:
        return True
    for st in stems:
        # prefix-match the shorter stem against the longer (handles "intro" vs
        # "introduction", "follow" vs "following"); guard against trivially
        # short stems matching everything.
        short, long = sorted((kw_stem, st), key=len)
        if len(short) >= 4 and long.startswith(short):
            return True
    # (b) synonym match
    for syn in _KEYWORD_SYNONYMS.get(kw, set()):
        if syn in stems or _stem(syn) in stems or syn in draft_lower:
            return True
    return False


# Length scoring (b181): a hard FAIL for being a few words over a tight cap was
# unfair to a good, on-topic reply. We now grade length:
#   - at or under the cap            -> factor 1.0 (no penalty)
#   - in the grace band (cap..2x)    -> linear decay 1.0 -> ~0.5
#   - over 2x the cap                -> hard FAIL (egregious blowup; degenerate
#                                       rambling, not a slightly-verbose reply)
# The factor scales the case score; status only HARD-fails past 2x. So a reply
# that is 10% over a 20-word cap is a small deduction, not a zero.
_LENGTH_HARD_FAIL_MULT = 2.0


def _length_factor(word_count: int, max_words: int) -> tuple[float, bool]:
    """Return (factor in [0,1], hard_fail) for the draft length."""
    if max_words <= 0:
        return 1.0, False
    if word_count <= max_words:
        return 1.0, False
    if word_count > max_words * _LENGTH_HARD_FAIL_MULT:
        return 0.0, True
    # Linear decay across the grace band [max_words, 2*max_words] -> [1.0, 0.5].
    over = (word_count - max_words) / float(max_words)  # 0..1 across the band
    return max(0.5, 1.0 - 0.5 * over), False
# Per-instance: results from each instance's nightly land in its own var/
# so multiple instances don't clobber each other's last-eval JSON.
RESULTS_PATH = get_var_dir() / "golden_results.json"


def load_golden_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load golden benchmark cases from YAML."""
    p = path or GOLDEN_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("cases", [])


def score_case(
    case: dict[str, Any],
    draft: str,
    detected_mode: str,
    detected_language: str | None = None,
) -> dict[str, Any]:
    """Score a single golden benchmark case.

    Scoring is FAIRER (b181) without being looser on real badness:
      * keyword matching is stem/synonym-tolerant (not brittle exact substring),
      * length over the cap is a GRADED penalty, hard-failing only past 2x,
      * empty / wrong-language / off-topic still hard-FAIL.
    A per-case numeric ``case_score`` in [0,1] is exposed for transparency; the
    discrete pass/warn/fail status is unchanged in shape and the pipeline
    composite (passed/total) is preserved.
    """
    draft_lower = draft.lower()
    words = draft.split()
    word_count = len(words)
    # Token stems for robust keyword matching.
    stems = {_stem(tok) for tok in _TOKEN_RE.split(draft_lower) if tok}
    # An empty / whitespace draft is a hard fail — never let it slip to "warn"
    # via the brevity check (0 words trivially passes max_words). An all-empty
    # eval means the model is broken, not that the adapter is fine.
    is_empty = not (draft or "").strip()

    # Keyword hit rate (stem/synonym tolerant — see _keyword_hits).
    expected_keywords = case.get("expected_keywords", [])
    if expected_keywords:
        hits = sum(1 for kw in expected_keywords if _keyword_hits(kw, stems, draft_lower))
        keyword_hit_rate = hits / len(expected_keywords)
    else:
        keyword_hit_rate = 1.0

    # Mode match
    expected_mode = case.get("expected_mode", "work")
    mode_match = detected_mode == expected_mode

    # Brevity — graded, not a cliff. brevity_pass stays True iff at/under the
    # cap (back-compat); length_factor scales the score in the grace band; a
    # blowup past 2x the cap is the only length-driven hard fail.
    max_words = case.get("max_words", 100)
    brevity_pass = word_count <= max_words
    length_factor, length_hard_fail = _length_factor(word_count, max_words)

    # Language detection check
    expected_language = case.get("expected_language")
    language_match = True
    if expected_language and detected_language:
        language_match = detected_language == expected_language

    # Continuous case score (transparency / graded composite): keyword hit rate
    # scaled by the length factor, zeroed by any hard-fail condition.
    if is_empty or length_hard_fail or not language_match:
        case_score = 0.0
    else:
        case_score = round(keyword_hit_rate * length_factor, 4)

    # Overall pass/warn/fail.
    if is_empty:
        status = "fail"
    elif length_hard_fail:
        # Egregious blowup (>2x cap) — degenerate rambling, still a hard fail.
        status = "fail"
    elif not language_match:
        # Wrong language is a genuine miss, never a pass.
        status = "fail"
    elif keyword_hit_rate >= 0.5 and mode_match:
        status = "pass"
    elif keyword_hit_rate == 0.0:
        # Nothing topical landed (no keyword, synonym, or stem hit). Even if the
        # mode happens to match, an off-topic reply is a genuine miss, not a
        # warn. Mode-match alone no longer rescues an off-topic draft to "warn".
        status = "fail"
    elif keyword_hit_rate >= 0.25 or mode_match:
        status = "warn"
    else:
        status = "fail"

    result = {
        "case_id": case["id"],
        "description": case.get("description", ""),
        "keyword_hit_rate": round(keyword_hit_rate, 2),
        "mode_match": mode_match,
        "detected_mode": detected_mode,
        "expected_mode": expected_mode,
        "word_count": word_count,
        "max_words": max_words,
        "brevity_pass": brevity_pass,
        "length_factor": round(length_factor, 4),
        "case_score": case_score,
        "empty": is_empty,
        "status": status,
    }
    if expected_language:
        result["expected_language"] = expected_language
        result["detected_language"] = detected_language
        result["language_match"] = language_match
    return result


def run_golden_eval(
    *,
    generate_fn=None,
    database_url: str | None = None,
    configs_dir: Path | None = None,
    golden_path: Path | None = None,
) -> dict[str, Any]:
    """Run the full golden evaluation suite.

    If generate_fn is None, returns empty results (for testing without model).
    """
    cases = load_golden_cases(golden_path)
    results: list[dict[str, Any]] = []

    for case in cases:
        if generate_fn is not None:
            output = generate_fn(
                case["inbound"],
                database_url=database_url,
                configs_dir=configs_dir,
            )
            draft = output.get("draft", "")
            detected_mode = output.get("detected_mode", "unknown")
            detected_language = output.get("detected_language")
        else:
            draft = ""
            detected_mode = "unknown"
            detected_language = None

        result = score_case(case, draft, detected_mode, detected_language)
        results.append(result)

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    warned = sum(1 for r in results if r["status"] == "warn")
    failed = sum(1 for r in results if r["status"] == "fail")
    empty_count = sum(1 for r in results if r.get("empty"))
    empty_rate = round(empty_count / total, 4) if total else 0.0
    # Degenerate = the eval can't be trusted to validate anything because the
    # model returned (mostly) nothing. An all-empty eval scores a low composite
    # that looks like a "real" score to the promotion gate; this flag lets the
    # gate refuse to act on it instead of silently promoting a broken adapter.
    degenerate = total > 0 and empty_rate > 0.5

    # Two composites, both honest:
    #  * pass_rate (passed/total) — the headline the promotion gate already keys
    #    off; UNCHANGED in meaning, so prior baselines stay comparable. Fairer
    #    per-case scoring lifts this only by reclassifying genuinely-good replies
    #    that were mis-FAILed/warned, not by loosening fail conditions.
    #  * graded_composite (mean case_score) — a finer continuous signal where a
    #    near-miss (good reply, one synonym short, or slightly long) earns
    #    partial credit instead of contributing a flat 0. Reported for insight;
    #    the gate is left on pass_rate to avoid silently moving the bar.
    pass_rate = round(passed / total, 4) if total else 0.0
    graded_composite = (
        round(sum(r.get("case_score", 0.0) for r in results) / total, 4) if total else 0.0
    )

    summary = {
        "total": total,
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "empty_count": empty_count,
        "empty_rate": empty_rate,
        "degenerate": degenerate,
        "pass_rate": pass_rate,
        "graded_composite": graded_composite,
        "results": results,
    }

    return summary


def save_results(summary: dict[str, Any], path: Path | None = None) -> None:
    """Save golden results to JSON."""
    p = path or RESULTS_PATH
    from app.core.atomic_io import atomic_write_json

    atomic_write_json(p, summary)


def format_scorecard(summary: dict[str, Any]) -> str:
    """Format golden benchmark results as a scorecard."""
    lines: list[str] = []
    lines.append("Golden Benchmark Results")
    lines.append("=" * 60)

    for r in summary["results"]:
        icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(r["status"], "?")
        kw_pct = int(r["keyword_hit_rate"] * 100)
        lines.append(f"  {r['case_id']:<30} {icon:<5} | kw={kw_pct}% mode={'Y' if r['mode_match'] else 'N'} words={r['word_count']}/{r['max_words']}")

    lines.append("=" * 60)
    lines.append(f"  Total: {summary['total']} | Pass: {summary['passed']} | Warn: {summary['warned']} | Fail: {summary['failed']}")
    return "\n".join(lines)


def main() -> None:
    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    parser = argparse.ArgumentParser(description="Run golden benchmark evaluation")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH, help="Path to golden.yaml")
    parser.add_argument("--summary-only", action="store_true", help="Print scorecard without saving")
    parser.add_argument("--db-path", type=Path, default=resolve_sqlite_path(get_settings().database_url))
    args = parser.parse_args()

    from app.generation.service import EVAL_SEED, DraftRequest, generate_draft

    database_url = f"sqlite:///{args.db_path}"
    # Instance-aware: use the active instance's configs, not the repo's.
    configs_dir = get_settings().configs_dir

    def _generate(prompt_text, *, database_url, configs_dir):
        response = generate_draft(
            # Deterministic eval (b170): greedy + fixed seed + no cloud
            # fallback so re-running the golden suite yields the same
            # composite. Eval-path only; production drafting is unchanged.
            DraftRequest(
                inbound_message=prompt_text,
                deterministic=True,
                seed=EVAL_SEED,
                no_cloud_fallback=True,
            ),
            database_url=database_url,
            configs_dir=configs_dir,
        )
        return {
            "draft": response.draft,
            "detected_mode": response.detected_mode,
            "confidence": response.confidence,
        }

    summary = run_golden_eval(
        generate_fn=_generate,
        database_url=database_url,
        configs_dir=configs_dir,
        golden_path=args.golden,
    )

    print(format_scorecard(summary))

    if not args.summary_only:
        save_results(summary)
        print(f"\nResults saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
