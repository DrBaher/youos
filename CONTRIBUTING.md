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
6. Submit a PR with a clear description of the change

## Architecture overview

- `app/` — FastAPI application (web server, API routes, core utilities)
- `scripts/` — CLI tools and pipeline scripts
- `configs/` — YAML configuration files (persona, prompts, retrieval)
- `templates/` — HTML templates for the web UI
- `tests/` — Test suite

## Reporting issues

Use the issue templates on GitHub for bug reports and feature requests.
