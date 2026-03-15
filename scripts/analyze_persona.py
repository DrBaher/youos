#!/usr/bin/env python3
"""Analyze the reply corpus to extract persona patterns.

Queries the reply_pairs table and produces a structured report plus
configs/persona_analysis.json with real observed patterns.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

from app.core.config import get_internal_domains, get_user_names

ROOT_DIR = Path(__file__).resolve().parents[1]


def _build_signature_patterns() -> list[re.Pattern]:
    """Build signature patterns from config user names plus common closings."""
    patterns: list[re.Pattern] = []
    # Add user-name-based signature patterns from config
    for name in get_user_names():
        if name.strip():
            patterns.append(re.compile(rf"^{re.escape(name)}", re.MULTILINE))
    # Common signature separators and closings
    patterns.extend([
        re.compile(r"^-- $", re.MULTILINE),
        re.compile(r"^--$", re.MULTILINE),
        re.compile(r"^Best,\s*$", re.MULTILINE),
        re.compile(r"^Cheers,\s*$", re.MULTILINE),
        re.compile(r"^Regards,\s*$", re.MULTILINE),
        re.compile(r"^Kind regards,\s*$", re.MULTILINE),
        re.compile(r"^Thanks,\s*$", re.MULTILINE),
        re.compile(r"^Thank you,\s*$", re.MULTILINE),
        re.compile(r"^Sent from my iPhone", re.MULTILINE),
        re.compile(r"^Sent from my iPad", re.MULTILINE),
    ])
    return patterns


# Signature patterns — truncate at first match
_SIGNATURE_PATTERNS = _build_signature_patterns()


def strip_signature(text: str) -> str:
    """Strip signature from reply text."""
    earliest_pos = len(text)
    found = False
    for pattern in _SIGNATURE_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()
            found = True
    if found:
        return text[:earliest_pos].rstrip()
    return text


def _extract_greeting(text: str) -> str | None:
    """Extract the greeting pattern from first line."""
    first_line = text.strip().split("\n")[0].strip()
    if not first_line:
        return None
    # Normalize
    lower = first_line.lower()
    if lower.startswith("hi ") or lower == "hi":
        return "Hi X"
    if lower.startswith("hey ") or lower == "hey":
        return "Hey X"
    if lower.startswith("hello ") or lower == "hello":
        return "Hello X"
    if lower.startswith("dear "):
        return "Dear X"
    if lower.startswith("thanks") or lower.startswith("thank you"):
        return "Thanks opener"
    if lower.startswith("sure") or lower.startswith("yes") or lower.startswith("no"):
        return "Direct answer"
    return "Direct start"


def _extract_closer(text: str) -> str | None:
    """Extract the closing pattern from last meaningful line."""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
    last = lines[-1].lower()
    if last.startswith("cheers"):
        return "Cheers"
    if last.startswith("best"):
        return "Best"
    if last.startswith("thanks"):
        return "Thanks"
    if last.startswith("regards"):
        return "Regards"
    if last.startswith("let me know"):
        return "Let me know"
    if "?" in last:
        return "Question"
    return "Statement"


def _classify_sender_type(author: str | None) -> str:
    """Sender classification using configured internal domains."""
    if not author:
        return "unknown"
    lower = author.lower()
    internal_domains = get_internal_domains()
    for domain in internal_domains:
        if f"@{domain}" in lower:
            return "internal"
    personal_domains = {"gmail.com", "yahoo.com", "hotmail.com", "icloud.com", "outlook.com"}
    for d in personal_domains:
        if f"@{d}" in lower:
            return "personal"
    return "external_client"


def analyze(db_path: Path) -> dict:
    """Run corpus analysis and return findings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT reply_text, inbound_author, reply_author, metadata_json FROM reply_pairs"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"error": "No reply pairs found", "total_pairs": 0}

    word_counts = []
    greeting_counter: Counter = Counter()
    closer_counter: Counter = Counter()
    signature_counter: Counter = Counter()
    tone_by_type: dict[str, list[int]] = {
        "internal": [], "external_client": [], "personal": [], "unknown": []
    }

    for row in rows:
        reply_raw = row["reply_text"] or ""
        stripped = strip_signature(reply_raw)

        # Detect which signature was found
        for pattern in _SIGNATURE_PATTERNS:
            if pattern.search(reply_raw):
                sig_name = pattern.pattern.replace("^", "").replace("\\s*$", "").replace("$", "").strip()
                signature_counter[sig_name] += 1
                break
        else:
            signature_counter["(none)"] += 1

        words = stripped.split()
        wc = len(words)
        word_counts.append(wc)

        greeting = _extract_greeting(stripped)
        if greeting:
            greeting_counter[greeting] += 1

        closer = _extract_closer(stripped)
        if closer:
            closer_counter[closer] += 1

        sender_type = _classify_sender_type(row["inbound_author"])
        tone_by_type.setdefault(sender_type, []).append(wc)

    # Compute stats
    word_counts_sorted = sorted(word_counts)
    n = len(word_counts_sorted)

    def percentile(data: list[int], p: float) -> int:
        idx = int(len(data) * p)
        return data[min(idx, len(data) - 1)]

    findings = {
        "total_pairs": len(rows),
        "reply_length": {
            "avg_words": round(statistics.mean(word_counts), 1) if word_counts else 0,
            "p25": percentile(word_counts_sorted, 0.25),
            "p50": percentile(word_counts_sorted, 0.50),
            "p75": percentile(word_counts_sorted, 0.75),
            "p95": percentile(word_counts_sorted, 0.95),
            "min": word_counts_sorted[0] if word_counts_sorted else 0,
            "max": word_counts_sorted[-1] if word_counts_sorted else 0,
        },
        "greeting_patterns": dict(greeting_counter.most_common(20)),
        "closing_patterns": dict(closer_counter.most_common(20)),
        "signature_patterns": dict(signature_counter.most_common(20)),
        "tone_by_sender_type": {
            k: {
                "count": len(v),
                "avg_words": round(statistics.mean(v), 1) if v else 0,
                "p50": percentile(sorted(v), 0.50) if v else 0,
            }
            for k, v in tone_by_type.items()
            if v
        },
    }

    return findings


def print_report(findings: dict) -> None:
    """Print a human-readable report."""
    print("=" * 60)
    print("YouOS Persona Corpus Analysis")
    print("=" * 60)

    if "error" in findings:
        print(f"\n{findings['error']}")
        return

    print(f"\nTotal reply pairs analyzed: {findings['total_pairs']}")

    rl = findings["reply_length"]
    print(f"\n--- Reply Length Distribution ---")
    print(f"  Average words: {rl['avg_words']}")
    print(f"  p25: {rl['p25']}  p50: {rl['p50']}  p75: {rl['p75']}  p95: {rl['p95']}")
    print(f"  Range: {rl['min']} - {rl['max']}")

    print(f"\n--- Greeting Patterns (top 20) ---")
    for pattern, count in findings["greeting_patterns"].items():
        print(f"  {pattern}: {count}")

    print(f"\n--- Closing Patterns (top 20) ---")
    for pattern, count in findings["closing_patterns"].items():
        print(f"  {pattern}: {count}")

    print(f"\n--- Signature Detection ---")
    for sig, count in findings["signature_patterns"].items():
        print(f"  {sig}: {count}")

    print(f"\n--- Tone by Sender Type ---")
    for stype, stats in findings["tone_by_sender_type"].items():
        print(f"  {stype}: {stats['count']} replies, avg {stats['avg_words']} words, p50 {stats['p50']}")

    print("\n" + "=" * 60)


def main() -> None:
    import sys
    sys.path.insert(0, str(ROOT_DIR))

    from app.core.settings import get_settings
    from app.db.bootstrap import resolve_sqlite_path

    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)

    findings = analyze(db_path)
    print_report(findings)

    output_path = ROOT_DIR / "configs" / "persona_analysis.json"
    output_path.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFindings written to {output_path}")


if __name__ == "__main__":
    main()
