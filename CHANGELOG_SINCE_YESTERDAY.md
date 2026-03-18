# Changelog Since Yesterday's Submission

Assuming yesterday’s ClawHub submission corresponds to commit `6942cbd` (release prep for v0.1.10).

## Included commits (`6942cbd..4a58db3`)

### 1) CI safety fix
- `75f71c5` — **fix(ci): narrow low-signal filter to avoid dropping valid training pairs**

### 2) Draft quality improvements
- `fe80fd7` — **feat(quality): implement sender-type style anchors and exemplar cache**
  - Adds sender-type style anchoring in draft prompting.
  - Adds exemplar cache logic for more consistent draft selection.

### 3) Persistence + onboarding defaults
- `d336c5b` — **feat(YouOS): Implement persistent exemplar cache and quickstart default; fix syntax**
  - Persists exemplar cache in SQLite (`exemplar_cache` table) across restarts.
  - Wires cache read/write/clear through API + generation paths.
  - Sets quickstart-first onboarding defaults.
  - Fixes syntax issue in generation service.

### 4) Dashboard metrics visibility
- `4a58db3` — **feat(YouOS): Display edit-reduction metrics in dashboard**
  - Surfaces edit-distance and high-rating deltas in stats UI.

## Test status (targeted)
- `.venv/bin/pytest -q tests/test_style_anchor_cache.py tests/test_config.py tests/test_setup_wizard.py tests/test_generation_improvements.py`
- Result: **35 passed**

## Files touched in this delta
- `app/api/feedback_routes.py`
- `app/api/stream_routes.py`
- `app/db/bootstrap.py`
- `app/generation/service.py`
- `scripts/setup_wizard.py`
- `templates/stats.html`
- `youos_config.yaml`
