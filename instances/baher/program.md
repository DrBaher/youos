# BaherOS Autoresearch — program.md

You are an autonomous research agent optimizing BaherOS.
Read this file carefully. Follow the loop exactly.

---

## What you are optimizing

BaherOS is a retrieval-first draft engine. It retrieves relevant past replies from a corpus of ~12,000 real email reply pairs and generates a Baher-style draft.

Your goal: improve the **composite score** on the benchmark suite.

---

## The fitness metric

Run the benchmark:
```bash
source .venv/bin/activate && python3 scripts/run_eval.py --tag autoresearch_<iteration>
```

The final line prints:
```
Total: 15 | Pass: X | Warn: Y | Fail: Z
Avg confidence: A | Avg keyword hit: B
```

**Composite score = 0.5 × (Pass/15) + 0.3 × avg_keyword_hit + 0.2 × avg_confidence**

Lower fail count and higher composite = better. Track composite to 4 decimal places.

---

## What you may modify

**ONLY these two files:**

1. `configs/retrieval/defaults.yaml` — retrieval tuning knobs:
   - `top_k_reply_pairs` (int, range 3–12)
   - `top_k_chunks` (int, range 1–8)
   - `top_k_documents` (int, range 1–6)
   - `recency_boost_days` (int, range 30–365)
   - `recency_boost_weight` (float, range 0.0–0.5, step 0.05)
   - `account_boost_weight` (float, range 0.0–0.4, step 0.05)

2. `configs/prompts.yaml` — only the `drafting_prompt` field (NOT `system_prompt`)

**DO NOT modify:**
- Any Python source files
- `configs/persona.yaml`
- `configs/prompts.yaml` → `system_prompt`
- `fixtures/benchmark_cases.yaml`
- `docs/schema.sql`
- Any test files

---

## The loop

For each iteration:

1. **Read** the current config files
2. **Propose one small change** — one knob, one step at a time
3. **Write** the change to the config file
4. **Run** the benchmark: `source .venv/bin/activate && python3 scripts/run_eval.py --tag autoresearch_<N>`
5. **Parse** the composite score from the output
6. **Compare** to baseline:
   - If composite improved by ≥ 0.02 → **KEEP**, log it, update baseline
   - If neutral or worse → **REVERT** the file to its previous state exactly, log it
7. **Log** each iteration to `autoresearch_log.md` (see format below)
8. **Repeat** — NEVER STOP until you have completed 80 iterations

---

## Logging format (`autoresearch_log.md`)

Append after each iteration:

```
## Iteration N — <timestamp>
- Surface: <config field name>
- Change: <old_value> → <new_value>
- Composite: <baseline> → <candidate>
- Outcome: KEPT / REVERTED
- Notes: <one line>
```

---

## Starting state

Before your first experiment, establish a baseline:
1. Run `source .venv/bin/activate && python3 scripts/run_eval.py --tag autoresearch_baseline`
2. Compute baseline composite
3. Log it as "Iteration 0 — Baseline" in `autoresearch_log.md`
4. Then begin iteration 1

---

## Rules

- One change per iteration — never change two things at once
- Always revert cleanly — the file must be byte-identical to pre-change state if reverting
- Never skip an iteration — even if you're unsure, try something and measure
- Run all 15 benchmark cases every time — no shortcuts
- Do not modify the benchmark cases to game the score
- NEVER STOP until 15 iterations are complete

---

## When done

After 80 iterations:
1. Print a final summary to `autoresearch_log.md`
2. Run: `openclaw system event --text "BaherOS Autoresearch complete: <N> improvements kept, final composite <score>" --mode now`
