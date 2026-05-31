"""Export feedback pairs to JSONL for MLX chat fine-tuning."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import yaml

from app.core.settings import get_settings
from app.db.bootstrap import resolve_sqlite_path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "feedback"
CONFIGS_DIR = ROOT_DIR / "configs"

# Above this many qualified pairs, skip the O(n^2) near-duplicate dedup — it
# would otherwise stall fine-tuning for many minutes on a large organic corpus.
DEDUP_MAX_PAIRS = 2000

# The inbound text is attacker-influenced (a sender controls their body). Bound
# the length fed to each O(n^2) hybrid_similarity comparison so a few huge bodies
# can't make every comparison expensive. 2000 chars is plenty to detect a
# near-duplicate (the threshold is 0.95).
DEDUP_TEXT_CAP = 2000


def _chmod_600(path) -> None:
    """Best-effort 0o600 — exported JSONL holds raw email bodies + drafts and
    must not be world-readable on a shared host (mirrors secure_io / b134)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- training-data sanitization / poison screen (b153) -----------------------
# The inbound side of every pair is attacker-influenced (a sender writes their
# own email body) and flows verbatim into the LoRA fine-tuning corpus. A crafted
# email could carry prompt-injection text, jailbreak markers, raw control bytes,
# or chat-template role tokens that — once trained in — persist in the adapter
# across restarts and steer EVERY future draft. The model only drafts
# human-reviewed replies, but a human may not notice e.g. an injected payment
# line, so we keep poison out of the corpus entirely rather than rely on review.

# Per-field char cap: one training example never needs more than this, and it
# bounds how much attacker text lands in a single record.
MAX_FIELD_CHARS = 8000

# Hard upper bound on exported pairs. A large/old mailbox organically accumulates
# tens of thousands of pairs which, 3x-oversampled, write a multi-GB train.jsonl
# that can fill disk and wedge the nightly launchd finetune. Keep the highest-
# quality, most-recent N.
MAX_EXPORT_PAIRS = 5000

# Control bytes are stripped except tab (09), newline (0a), carriage return (0d).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Chat-template / role-control tokens must never appear inside a training field —
# a sender embedding "<|im_start|>system ..." would inject a spoofed turn into
# the tokenized example.
_TEMPLATE_TOKENS = (
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "<|user|>",
    "<|assistant|>",
    "<<sys>>",
    "<</sys>>",
    "[inst]",
    "[/inst]",
)

_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above)\s+(?:instructions|context|text)"
    r"|you\s+are\s+now\s+(?:in\s+)?(?:an?\s+)?(?:unrestricted|developer|dan|jailbroken|god)"
    r"|system\s+(?:prompt\s+)?override"
    r"|forget\s+(?:all\s+|everything\s+)?(?:you|your|previous|prior)",
    re.IGNORECASE,
)


def sanitize_training_text(text: str | None) -> str:
    """Strip control bytes (keep \\t \\n \\r) and cap length.

    Applied at the build_record / DPO sink so no caller can write raw attacker
    control bytes into a training example — they survive the JSON round-trip and
    reach the tokenizer unchanged."""
    if not text:
        return ""
    cleaned = _CTRL_RE.sub("", text)
    if len(cleaned) > MAX_FIELD_CHARS:
        cleaned = cleaned[:MAX_FIELD_CHARS]
    return cleaned


def is_poisoned_text(text: str | None) -> bool:
    """True if text carries prompt-injection / jailbreak markers or chat-template
    role tokens. Such a pair is DROPPED from the corpus rather than trained on."""
    if not text:
        return False
    low = text.lower()
    if any(tok in low for tok in _TEMPLATE_TOKENS):
        return True
    return bool(_INJECTION_RE.search(text))


def parse_args() -> argparse.Namespace:
    # Same shape as scripts/finetune_lora.py: defaults resolved at call time
    # so YOUOS_DATA_DIR set on the calling shell (e.g. the nightly under
    # launchd) lands the export's DB read on the active instance.
    default_db = resolve_sqlite_path(get_settings().database_url)
    p = argparse.ArgumentParser(description="Export feedback pairs to JSONL")
    p.add_argument("--all", action="store_true", help="Export all pairs, not just unused")
    p.add_argument("--since", type=str, default=None, help="Only pairs created after this date (YYYY-MM-DD)")
    p.add_argument("--output", type=str, default=None, help="Output file path (default: data/feedback/train.jsonl)")
    p.add_argument("--min-rating", type=int, default=3, help="Minimum rating to include (default: 3)")
    p.add_argument("--min-edit-pct", type=float, default=0.05, help="Minimum edit distance pct (default: 0.05)")
    p.add_argument("--db", type=str, default=str(default_db), help="Database path")
    p.add_argument("--no-persona", action="store_true", help="Use bare format without persona/system prompt")
    p.add_argument("--configs-dir", type=str, default=str(CONFIGS_DIR), help="Configs directory")
    p.add_argument("--dpo", action="store_true", help="Export DPO preference pairs (chosen/rejected)")
    p.add_argument(
        "--human-rated-only",
        action="store_true",
        help="Exclude self-labeled rows (auto-captured / organic) — keep only human review-queue ratings",
    )
    p.add_argument("--curriculum", action=argparse.BooleanOptionalAction, default=True, help="Sort first 20%% by quality (curriculum learning)")
    p.add_argument("--no-dedup", action="store_true", help="Disable near-duplicate deduplication")
    p.add_argument(
        "--persona",
        type=str,
        default=None,
        help=(
            "Filter to one sender_type cohort for per-persona fine-tuning "
            "(e.g. --persona internal). Omit to export all cohorts mixed "
            "together for the global adapter. Used by Phase 2 of the "
            "per-persona adapters work."
        ),
    )
    return p.parse_args()


def _load_persona(configs_dir: Path) -> dict:
    path = configs_dir / "persona.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_prompts(configs_dir: Path) -> dict:
    path = configs_dir / "prompts.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_system_message(persona: dict, prompts: dict) -> str:
    """Build a system message combining system_prompt and persona preamble."""
    system_prompt = prompts.get("system_prompt", "You are YouOS, a local-first personal email copilot.").strip()

    style = persona.get("style", {})
    voice = style.get("voice")
    avg_words = style.get("avg_reply_words")
    greeting_patterns = persona.get("greeting_patterns", {})
    closing_patterns = persona.get("closing_patterns", {})

    preamble_parts: list[str] = []
    if voice:
        preamble_parts.append(f"Voice style: {voice}.")
    if avg_words:
        preamble_parts.append(f"Target reply length: ~{avg_words} words.")
    if greeting_patterns:
        greetings = ", ".join(f"{k}: {v}" for k, v in greeting_patterns.items() if k != "default")
        if greetings:
            preamble_parts.append(f"Greeting patterns: {greetings}.")
    if closing_patterns:
        closings = ", ".join(f"{k}: {v}" for k, v in closing_patterns.items() if k != "default")
        if closings:
            preamble_parts.append(f"Closing patterns: {closings}.")

    if preamble_parts:
        return system_prompt + "\n\n" + "\n".join(preamble_parts)
    return system_prompt


def build_record(
    inbound: str,
    edited_reply: str,
    *,
    system_message: str | None = None,
) -> dict:
    """Build a JSONL record with optional system message.

    Sanitizes the user/assistant content at this single sink so no caller can
    write raw attacker control bytes into a training example (b153). The system
    message is internally generated (trusted) and passed through as-is."""
    messages = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": sanitize_training_text(inbound)})
    messages.append({"role": "assistant", "content": sanitize_training_text(edited_reply)})
    return {"messages": messages}


def export_dpo(args: argparse.Namespace) -> None:
    """Export DPO preference pairs: chosen (rating >= 4) vs rejected (rating <= 2)."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # b153: never let attacker-influenced, self-labeled content drive the
        # 'chosen' preference gradient. Exclude auto-captured / organic rows from
        # the chosen tier so only genuinely human-rated replies are preferred.
        # Column-guarded so older DBs that predate these columns still export.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_pairs)")}
        chosen_extra = ""
        if "feedback_note" in cols:
            chosen_extra += " AND COALESCE(feedback_note, '') NOT LIKE 'auto-captured%'"
        if "organic" in cols:
            chosen_extra += " AND COALESCE(organic, 0) = 0"
        chosen_rows = conn.execute(
            "SELECT inbound_text, edited_reply, rating FROM feedback_pairs "
            "WHERE rating >= 4 AND LENGTH(edited_reply) >= 15" + chosen_extra
        ).fetchall()
        rejected_rows = conn.execute(
            "SELECT inbound_text, edited_reply, rating FROM feedback_pairs WHERE rating <= 2 AND LENGTH(edited_reply) >= 15"
        ).fetchall()
    finally:
        conn.close()

    if not chosen_rows or not rejected_rows:
        print(f"Not enough DPO pairs: {len(chosen_rows)} chosen, {len(rejected_rows)} rejected")
        return

    pairs: list[dict] = []
    used_rejected: set[int] = set()

    for chosen in chosen_rows:
        c_len = len(chosen["inbound_text"] or "")
        if c_len == 0:
            continue
        # b153: never train on a poisoned chosen example.
        if is_poisoned_text(chosen["inbound_text"]) or is_poisoned_text(chosen["edited_reply"]):
            continue
        for j, rejected in enumerate(rejected_rows):
            if j in used_rejected:
                continue
            r_len = len(rejected["inbound_text"] or "")
            if r_len == 0:
                continue
            if is_poisoned_text(rejected["edited_reply"]):
                continue
            # Match by similar inbound length (within 50%)
            ratio = min(c_len, r_len) / max(c_len, r_len)
            if ratio >= 0.5:
                pairs.append(
                    {
                        "prompt": sanitize_training_text(chosen["inbound_text"]),
                        "chosen": sanitize_training_text(chosen["edited_reply"]),
                        "rejected": sanitize_training_text(rejected["edited_reply"]),
                    }
                )
                used_rejected.add(j)
                break
        if len(pairs) >= MAX_EXPORT_PAIRS:  # bound emitted pairs (disk budget)
            break

    if not pairs:
        print("No DPO pairs could be matched.")
        return

    output_path = ROOT_DIR / "data" / "dpo_train.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for pair in pairs:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")
    _chmod_600(output_path)  # contains raw email bodies — not world-readable

    print(f"Exported {len(pairs)} DPO pairs to {output_path}")


def deduplicate_pairs(
    qualified: list[tuple],
    threshold: float = 0.95,
) -> tuple[list[tuple], int]:
    """Deduplicate pairs by inbound text similarity.

    If two pairs have hybrid_similarity >= threshold on their inbound text,
    keep only the one with higher quality score (or more recent if tied).
    Returns (deduped list, number removed).
    """
    from app.core.diff import hybrid_similarity

    if len(qualified) <= 1:
        return qualified, 0

    # O(n^2) hybrid_similarity over every pair — fine for a review-queue-sized
    # set, pathological for a large organic corpus (tens of thousands of pairs),
    # where it runs for many minutes and stalls the wizard's / nightly's
    # fine-tune. Above the cap the marginal cleanup isn't worth the cost.
    if len(qualified) > DEDUP_MAX_PAIRS:
        return qualified, 0

    keep = list(qualified)
    removed = 0
    i = 0
    while i < len(keep):
        j = i + 1
        while j < len(keep):
            # Bound the text length so a huge attacker body can't make each of
            # the O(n^2) comparisons expensive.
            sim = hybrid_similarity(keep[i][1][:DEDUP_TEXT_CAP], keep[j][1][:DEDUP_TEXT_CAP])
            if sim >= threshold:
                # Keep the one with higher quality; if tied, keep more recent (later in list)
                q_i, q_j = keep[i][3], keep[j][3]
                if q_j > q_i:
                    keep.pop(i)
                else:
                    keep.pop(j)
                removed += 1
                continue  # don't increment j since we removed an element
            j += 1
        i += 1
    return keep, removed


def _is_low_signal_pair(inbound: str, edited_reply: str) -> bool:
    """Return True when pair carries little learning signal for fine-tuning.

    Keep this conservative: only filter obvious acknowledgement / phatic exchanges.
    """
    import re

    low_signal_reply = re.compile(
        r"^\s*(ok|okay|k|sure|thanks|thank you|thx|ty|noted|got it|sounds good|perfect|great|hello|hi)\s*[.!]?\s*$",
        re.IGNORECASE,
    )

    inbound_text = (inbound or "").strip()
    reply_text = (edited_reply or "").strip()

    inbound_words = inbound_text.split()
    reply_words = reply_text.split()

    # Empty/tiny responses are generally poor supervision targets.
    if not reply_words:
        return True
    if low_signal_reply.match(reply_text):
        return True

    # Very short back-and-forths usually add little style signal.
    if len(inbound_words) <= 2 and len(reply_words) <= 2:
        return True

    return False


def export(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    configs_dir = Path(args.configs_dir)

    # Build system message from persona + prompts (unless --no-persona)
    system_message = None
    if not args.no_persona:
        persona = _load_persona(configs_dir)
        prompts = _load_prompts(configs_dir)
        system_message = _build_system_message(persona, prompts)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # `organic` pairs are real sent replies with no YouOS draft (see
        # extract_auto_feedback.py). Detect the column so older DBs that predate
        # it still export; absent → treated as non-organic (0).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_pairs)")}
        organic_expr = "COALESCE(organic, 0)" if "organic" in cols else "0"
        note_expr = "COALESCE(feedback_note, '')" if "feedback_note" in cols else "''"
        # b160: a row is "self-labeled" if it's organic or auto-captured (the
        # model's own sent-reply capture), i.e. NOT a human review-queue rating.
        self_labeled_expr = f"(CASE WHEN {organic_expr} = 1 OR {note_expr} LIKE 'auto-captured%' THEN 1 ELSE 0 END)"
        # Down-weight self-labeled rows to the non-oversampled tier (quality capped
        # at 3) so a benign attacker payload in a self-labeled reply isn't amplified
        # 2-3x into the adapter; human-rated rows keep their full rating. b153 added
        # this exclusion to the DPO path only — the default SFT export still pulled
        # them in at full weight.
        quality_expr = (
            f"CASE WHEN {self_labeled_expr} = 1 THEN MIN(COALESCE(rating, 3), 3) "
            "ELSE COALESCE(rating, 3) END as quality_score"
        )
        query = (
            "SELECT rowid as _rowid, inbound_text, edited_reply, rating, edit_distance_pct, created_at, "
            f"{organic_expr} as organic, {self_labeled_expr} as self_labeled, {quality_expr} "
            "FROM feedback_pairs WHERE 1=1"
        )
        params: list = []

        # Curated export: drop self-labeled rows entirely (only human ratings).
        if getattr(args, "human_rated_only", False):
            query += f" AND {self_labeled_expr} = 0"

        # `used_in_finetune` is a global flag — it gates the incremental
        # global-adapter loop ("don't retrain on data the global already saw").
        # Per-persona training should use the *entire* cohort every time
        # (each persona's adapter is smallish and retrains from scratch), so
        # `--persona` implicitly bypasses this filter. Otherwise the first
        # persona to train would steal pairs from the global / other personas.
        # ``getattr`` since some legacy callers (tests, direct invocations)
        # build a Namespace without the new attribute.
        persona_filter = getattr(args, "persona", None)
        if not args.all and not persona_filter:
            query += " AND used_in_finetune = 0"

        if args.since:
            query += " AND created_at >= ?"
            params.append(args.since)

        if persona_filter:
            # Filter to one sender_type cohort. We explicitly do NOT include
            # NULL-sender_type rows here — those are pre-Phase-1 historical
            # rows that haven't been backfilled, and we'd rather have a smaller
            # honest cohort than dilute it with un-classifiable data.
            query += " AND sender_type = ?"
            params.append(persona_filter.strip().lower())

        rows = conn.execute(query, params).fetchall()

        # b160: map each feedback_pair to the sender of its linked reply (via the
        # now-populated reply_pair_id) so one chatty correspondent can't dominate
        # the voice corpus. Rows with no linked sender are each their own implicit
        # sender (uncapped). A separate query avoids ambiguous-column joins in the
        # main SELECT; reply_pairs.inbound_author predates this and may be absent.
        author_by_rowid: dict[int, str] = {}
        rp_cols = {r[1] for r in conn.execute("PRAGMA table_info(reply_pairs)")}
        if "reply_pair_id" in cols and "inbound_author" in rp_cols:
            for fp_rowid, author in conn.execute(
                "SELECT fp.rowid, rp.inbound_author FROM feedback_pairs fp "
                "JOIN reply_pairs rp ON fp.reply_pair_id = rp.id "
                "WHERE rp.inbound_author IS NOT NULL AND rp.inbound_author != ''"
            ):
                author_by_rowid[fp_rowid] = str(author).strip().lower()
    finally:
        conn.close()

    if not rows:
        print("No matching feedback pairs found. Exported 0 pairs.")
        return

    # Quality filters
    min_rating = args.min_rating
    min_edit_pct = args.min_edit_pct
    qualified: list[tuple] = []
    filtered_count = 0
    null_rating_count = 0
    poisoned_count = 0

    for row in rows:
        rating = row["rating"]
        edit_pct = row["edit_distance_pct"]
        edited_reply = row["edited_reply"] or ""
        organic = row["organic"]

        # b153: drop attacker-injected pairs before they become training examples.
        # The inbound text is sender-controlled; screen it (and the reply) for
        # prompt-injection / jailbreak markers and chat-template role tokens so a
        # crafted email can't poison the adapter.
        if is_poisoned_text(row["inbound_text"]) or is_poisoned_text(edited_reply):
            poisoned_count += 1
            filtered_count += 1
            continue

        # Exclude pairs with short edited replies
        if len(edited_reply) < 15:
            filtered_count += 1
            continue

        # E20: quality gate — drop low-signal acknowledgement-only pairs
        if _is_low_signal_pair(row["inbound_text"] or "", edited_reply):
            filtered_count += 1
            continue

        # Handle null rating: include with warning
        if rating is None:
            null_rating_count += 1
        elif rating < min_rating:
            filtered_count += 1
            continue

        # Exclude pairs with low edit + not 5-star (no signal). The edit-distance
        # floor only makes sense for review-queue pairs, where a YouOS draft
        # existed to diff against. Organic pairs (real sent replies, no draft)
        # have edit_distance_pct=0 by construction — applying the floor would
        # discard the entire historical corpus, so a fresh user's first
        # fine-tune would see "No qualifying pairs". Exempt them.
        if not organic and edit_pct is not None and edit_pct < min_edit_pct and (rating is None or rating < 5):
            filtered_count += 1
            continue

        quality = row["quality_score"] if "quality_score" in row.keys() else (rating or 3)
        author = author_by_rowid.get(row["_rowid"]) if "_rowid" in row.keys() else None
        qualified.append((row["created_at"] or "", row["inbound_text"], edited_reply, quality, author))

    if null_rating_count > 0:
        print(f"Warning: {null_rating_count} pairs have null rating (included)")
    if poisoned_count > 0:
        print(f"Dropped {poisoned_count} pairs with prompt-injection / role-token markers (b153)")

    if not qualified:
        print(f"No qualifying pairs after filtering. Filtered out {filtered_count} low-quality pairs.")
        return

    # b160: per-sender cap — one chatty correspondent / newsletter-reply can't
    # dominate the voice corpus. Keep the highest-quality pairs per sender; rows
    # with no linked sender (author is None) are each their own implicit sender
    # and uncapped. Applied BEFORE the global truncation.
    max_per_sender = max(20, MAX_EXPORT_PAIRS // 20)
    qualified.sort(key=lambda x: (x[3], x[0]), reverse=True)  # quality DESC, created_at DESC
    per_sender: dict[str, int] = {}
    capped: list[tuple] = []
    sender_dropped = 0
    for item in qualified:
        author = item[4]
        if author:
            n = per_sender.get(author, 0)
            if n >= max_per_sender:
                sender_dropped += 1
                continue
            per_sender[author] = n + 1
        capped.append(item)
    if sender_dropped:
        print(f"Per-sender cap: dropped {sender_dropped} pairs (>{max_per_sender} from one sender)")
    qualified = capped

    # b153: bound the exported set so a large/old mailbox can't write a multi-GB
    # train.jsonl that fills disk and wedges the nightly. Keep the highest-
    # quality, most-recent pairs (quality DESC, then created_at DESC).
    if len(qualified) > MAX_EXPORT_PAIRS:
        qualified.sort(key=lambda x: (x[3], x[0]), reverse=True)
        print(f"Capping export at {MAX_EXPORT_PAIRS} pairs (had {len(qualified)}); keeping highest-quality/most-recent")
        qualified = qualified[:MAX_EXPORT_PAIRS]

    # E15: oversample 5-star recent pairs (last 90 days) 2-3x for stronger training signal
    from datetime import datetime, timedelta
    from datetime import timezone as _tz
    cutoff_90d = (datetime.now(_tz.utc) - timedelta(days=90)).isoformat()[:10]
    oversampled: list[tuple] = []
    for item in qualified:
        created_at, inbound, reply, quality, _author = item
        is_recent = (created_at or "")[:10] >= cutoff_90d
        rating_approx = int(round(quality))
        if rating_approx >= 5 and is_recent:
            oversampled.extend([item, item, item])  # 3x
        elif rating_approx >= 4 and is_recent:
            oversampled.extend([item, item])  # 2x
        else:
            oversampled.append(item)
    if len(oversampled) > len(qualified):
        print(f"E15 oversampling: {len(qualified)} -> {len(oversampled)} pairs (5-star/recent boosted)")
    qualified = oversampled

    # b153: clamp the post-oversample count too, so 3x boosting can't blow past
    # the disk budget on a large corpus.
    if len(qualified) > MAX_EXPORT_PAIRS:
        print(f"Clamping post-oversample {len(qualified)} -> {MAX_EXPORT_PAIRS} pairs (disk budget)")
        qualified = qualified[:MAX_EXPORT_PAIRS]

    print(f"Exported {len(qualified)} pairs (filtered out {filtered_count} low-quality pairs)")

    # Deduplication by inbound similarity (before temporal split)
    if not getattr(args, "no_dedup", False):
        if len(qualified) > DEDUP_MAX_PAIRS:
            print(f"Skipping near-duplicate dedup ({len(qualified)} pairs > {DEDUP_MAX_PAIRS} cap — O(n^2) too slow)")
        else:
            qualified, dedup_count = deduplicate_pairs(qualified)
            if dedup_count:
                print(f"Deduped {dedup_count} near-duplicate training pairs")

    # Temporal split: sort by created_at ASC, most recent 15% as validation
    qualified.sort(key=lambda x: x[0])

    # Curriculum learning: sort first 20% by quality_score ASC (warmup on easier examples)
    curriculum_applied = False
    warmup_count = 0
    if getattr(args, "curriculum", True):
        warmup_count = max(1, int(len(qualified) * 0.2))
        warmup = sorted(qualified[:warmup_count], key=lambda x: x[3])  # sort by quality ASC
        remainder = qualified[warmup_count:]
        qualified = warmup + remainder
        curriculum_applied = True
        print(f"Curriculum learning: warmup on first {warmup_count} easiest examples")

    records = [build_record(inbound, reply, system_message=system_message) for _, inbound, reply, _q, _a in qualified]

    # Prepend curriculum metadata line if applicable
    if curriculum_applied:
        meta_line = {"_curriculum": True, "warmup_count": warmup_count, "total": len(records)}
        records.insert(0, meta_line)

    val_count = max(1, min(20, int(len(records) * 0.15)))
    if len(records) <= 1:
        train = records
        valid = []
    else:
        train = records[:-val_count]
        valid = records[-val_count:]

    print(f"Train: {len(train)} pairs | Val: {len(valid)} pairs (temporal split, val = most recent 15%)")

    # Determine output paths
    if args.output:
        train_path = Path(args.output)
        valid_path = train_path.parent / "valid.jsonl"
    else:
        output_dir = DEFAULT_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        train_path = output_dir / "train.jsonl"
        valid_path = output_dir / "valid.jsonl"

    train_path.parent.mkdir(parents=True, exist_ok=True)

    with open(train_path, "w", encoding="utf-8") as f:
        for rec in train:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _chmod_600(train_path)

    with open(valid_path, "w", encoding="utf-8") as f:
        for rec in valid:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _chmod_600(valid_path)

    print(f"Exported {len(records)} pairs to {train_path}")
    print(f"  Train: {len(train)} pairs -> {train_path}")
    print(f"  Valid: {len(valid)} pairs -> {valid_path}")


def main() -> None:
    args = parse_args()
    if args.dpo:
        export_dpo(args)
    else:
        export(args)


if __name__ == "__main__":
    main()
