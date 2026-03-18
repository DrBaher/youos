# YouOS Release Guardrails (Development, Docs, Submission)

This checklist is mandatory before any public submission/update (ClawHub, GitHub release notes, listing refreshes).

## 1) Metadata consistency (must match reality)

- `SKILL.md` must describe YouOS as a **full local Python app**, not instruction-only.
- `clawhub.json` must declare runtime requirements:
  - `requires.bins`: `python3`, `gog`
  - `requires.platform`: `darwin`
  - `requires.arch`: `arm64`
- Version must be consistent across:
  - `pyproject.toml`
  - `clawhub.json`
  - `app/main.py`
  - `app/api/stats_routes.py`
  - UI footers/templates
  - `CHANGELOG.md`

## 2) Privacy/security defaults (no risky defaults)

- Default model fallback must be local-first safe:
  - `model.fallback: none` by default (external fallback opt-in)
- Default web bind must be local only:
  - `server.host: 127.0.0.1`
- Setup flow must not leave auth wide-open:
  - if user leaves PIN empty, generate a PIN automatically
- Docs must explicitly state:
  - external fallback may send email/context externally if enabled
  - strict local-only mode: set `model.fallback: none`

## 3) Documentation integrity

- Public docs/UI text must match shipped behavior.
- Remove stale references to removed features.
- Keep install path explicit (manual pip) and prerequisites clear.
- Keep examples aligned with current defaults (`127.0.0.1`, current version).

## 4) CI quality gate (required)

Before push/merge:
- `ruff check .` passes
- tests pass (at minimum changed scopes + known CI-sensitive suites)
- if modifying training/export filters, run:
  - `tests/test_export_quality_gate.py`
  - `tests/test_finetune_improvements.py`

## 5) Submission package hygiene

Use a review-friendly package that excludes non-essential noise:
- exclude caches, logs, runtime state, local instances, binaries/screenshots when form requires text-only
- include only essentials for scanner understanding:
  - `SKILL.md`, `clawhub.json`, `pyproject.toml`, `README.md`, `PRIVACY.md`, `app/`, needed `scripts/`, required `configs/`

## 6) Release process contract

- Every upload increments version (no reuse).
- Update `CHANGELOG.md` with concise, accurate bullets.
- Verify GitHub `main` contains the release commit before submission.
- If CI fails, patch and re-run before re-submitting.

## 7) Non-commit local state

Never commit local runtime state:
- `youos_config.yaml` (instance/local values)
- `var/`, `instances/`, `.venv/`, caches

---

If a change conflicts with this document, update this document in the same PR and explain why.
