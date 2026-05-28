# Contributing to YouOS

Thanks for your interest in contributing to YouOS!

## Development setup

```bash
# Clone the repo
git clone https://github.com/DrBaher/youos.git
cd youos

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in dev mode
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -q
```

## Running tests

```bash
python -m pytest tests/ -q          # all tests
python -m pytest tests/ -q -x       # stop on first failure
python -m pytest tests/test_auth.py  # specific file
```

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for linting:

```bash
ruff check .
ruff format .
```

Configuration is in `pyproject.toml` (line length 100, Python 3.11 target).

## Pull requests

1. Fork the repo and create a feature branch
2. Make your changes
3. Add tests for new functionality
4. Ensure all tests pass: `python -m pytest tests/ -q`
5. Ensure linting passes: `ruff check .`
6. Run the release checklist in `docs/RELEASE_GUARDRAILS.md` for any user-facing/docs/submission change
7. Submit a PR with a clear description of the change

`main` is branch-protected — PRs can't merge until both `test (3.11)` and `test (3.12)` CI matrix runs pass green.

## Verification checklists

These guardrails came from real bug classes that slipped through mocked tests in 2026-05. Add to them when you find a new one.

### Before merging code that shells out to an external CLI

Mocked subprocess tests verify the shape you *wrote*, not the shape the real CLI *wants*. b47/b48 shipped completely wrong `gog gmail drafts create` / `gws gmail drafts create` invocations because the implementations were "best-effort" guesses that the test mocks happily passed.

Required before merging:

- [ ] Run the real `<cmd> --help` (or the tool's schema introspection — `gws schema <method>`, `gcloud help`) on the target machine and confirm every flag name, every subcommand path, and every accepted argument shape.
- [ ] When schema introspection exists, prefer it over `--help` — it's machine-readable and complete.
- [ ] If the machine doesn't have an authenticated session for the CLI, schema-verification is a valid substitute. **Say so explicitly in the PR body** (b48 deferred live gws verification this way).
- [ ] Isolate every external-CLI invocation in a single function so the fix is one-line when the shape drifts.
- [ ] Add a comment at the top of that function naming the verification command for future readers.

### Before merging tests that mutate config

Module-level globals bind at import time. `monkeypatch.setenv` after the module imports is too late. b46 caught test fixtures writing to the *real* `youos_config.yaml` for this reason.

For any test that hits `set_flag(...)`, `/api/config/set`, or `/api/agent/skip_senders/promote`:

- [ ] Do BOTH `monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))` AND `monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")`.
- [ ] Also do `from app.core.config import load_config; load_config.cache_clear()` (the `@lru_cache(maxsize=1)` decorator holds the first-loaded config).
- [ ] After running the test suite locally, `git diff youos_config.yaml` — if your test name or test-fixture data appears in the diff, the fixture isn't isolated.

### Before merging tests that exercise model generation

The warm `mlx_lm.server` short-circuit (`app.core.model_server.is_enabled`) makes the cold-subprocess path unreachable on dev machines with an actual model server running. b50 fixed 4 tests that failed locally for this reason.

- [ ] If the test fixture-asserts on `subprocess.Popen` or `_run_subprocess` calls, also stub `model_server.is_enabled` (or `is_healthy`) to return False.

### Before merging anything that touches `sqlite:///` URLs

`urllib.parse.urlparse("sqlite:///var/youos.db").path` returns `/var/youos.db` (absolute) — silently breaks the relative-path default. Use `removeprefix("sqlite:///")` to match `app/db/bootstrap.py`.

- [ ] Grep for `urlparse.*\.path` after any change to DB-path resolution; should be zero hits.

## Architecture overview

- `app/` — FastAPI application (web server, API routes, core utilities)
- `scripts/` — CLI tools and pipeline scripts
- `configs/` — YAML configuration files (persona, prompts, retrieval)
- `templates/` — HTML templates for the web UI
- `tests/` — Test suite

## Reporting issues

Use the issue templates on GitHub for bug reports and feature requests.
