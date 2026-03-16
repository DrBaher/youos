"""Merge persona analysis results into configs/persona.yaml."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _dominant_pattern(counter: dict[str, int], threshold: float = 0.60) -> str | None:
    """Return the pattern if it exceeds threshold frequency, else None."""
    total = sum(counter.values())
    if total == 0:
        return None
    for pattern, count in sorted(counter.items(), key=lambda x: -x[1]):
        if count / total > threshold:
            return pattern
    return None


def merge_persona_analysis(
    *,
    analysis_path: Path | None = None,
    persona_path: Path,
    log_path: Path,
    dry_run: bool = False,
    findings_dict: dict[str, Any] | None = None,
) -> list[str]:
    """Merge persona analysis into persona.yaml.

    Returns list of changes made (or that would be made in dry_run).
    """
    if findings_dict is not None:
        findings = findings_dict
    elif analysis_path and analysis_path.exists():
        findings = json.loads(analysis_path.read_text(encoding="utf-8"))
    else:
        return []

    if not persona_path.exists():
        return []

    persona = yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}
    changes: list[str] = []

    # Update avg_reply_words if changed by >5 words
    new_avg = findings.get("reply_length", {}).get("avg_words")
    if new_avg is not None:
        current_avg = persona.get("style", {}).get("avg_reply_words")
        if current_avg is None or abs(new_avg - current_avg) > 5:
            changes.append(f"avg_reply_words: {current_avg} -> {round(new_avg)}")
            if not dry_run:
                persona.setdefault("style", {})["avg_reply_words"] = round(new_avg)

    # Update greeting_patterns if a new dominant pattern emerges (>60%)
    greeting_patterns = findings.get("greeting_patterns", {})
    dominant_greeting = _dominant_pattern(greeting_patterns)
    if dominant_greeting:
        current_default = persona.get("greeting_patterns", {}).get("default")
        if current_default != dominant_greeting:
            changes.append(f"greeting default: {current_default} -> {dominant_greeting}")
            if not dry_run:
                persona.setdefault("greeting_patterns", {})["default"] = dominant_greeting

    # Update closing_patterns similarly
    closing_patterns = findings.get("closing_patterns", {})
    dominant_closing = _dominant_pattern(closing_patterns)
    if dominant_closing:
        current_default = persona.get("closing_patterns", {}).get("default")
        if current_default != dominant_closing:
            changes.append(f"closing default: {current_default} -> {dominant_closing}")
            if not dry_run:
                persona.setdefault("closing_patterns", {})["default"] = dominant_closing

    # Never overwrite custom_constraints — they are user-set

    if changes:
        if dry_run:
            for change in changes:
                print(f"  [DRY RUN] Would change: {change}")
        else:
            persona_path.write_text(
                yaml.dump(persona, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            # Append to merge log
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).isoformat()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{timestamp}] Merged {len(changes)} change(s):\n")
                for change in changes:
                    f.write(f"  - {change}\n")
    else:
        if dry_run:
            print("  [DRY RUN] No changes needed")

    return changes
