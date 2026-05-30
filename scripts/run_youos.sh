#!/usr/bin/env bash
# YouOS launcher for direct use and launchd ProgramArguments.
set -euo pipefail

fatal() {
  echo "FATAL: $*" >&2
  exit 1
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || fatal "cannot cd to repo dir: $REPO_DIR"

export YOUOS_DATA_DIR="${YOUOS_DATA_DIR:-/Users/bbot/YouOS-Instances/baheros}"
if [[ -z "${YOUOS_DATA_DIR}" ]]; then
  fatal "YOUOS_DATA_DIR is empty"
fi
if [[ ! -d "$YOUOS_DATA_DIR" ]]; then
  fatal "YOUOS_DATA_DIR does not exist: $YOUOS_DATA_DIR"
fi

VENV_DIR="${YOUOS_VENV_DIR:-$REPO_DIR/.venv}"
PYTHON_BIN="${YOUOS_PYTHON_BIN:-$VENV_DIR/bin/python}"
if [[ ! -d "$VENV_DIR" ]]; then
  fatal "virtualenv directory not found: $VENV_DIR"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  fatal "python executable not found in virtualenv: $PYTHON_BIN"
fi

HOST="${YOUOS_HOST:-127.0.0.1}"
PORT="${YOUOS_PORT:-8765}"
APP_MODULE="${YOUOS_APP_MODULE:-app.main:app}"

# --limit-concurrency bounds total in-flight requests so a flood of the
# expensive draft endpoints can't exhaust the shared sync threadpool and freeze
# the single-worker server (generous ceiling for a single-user instance).
exec "$PYTHON_BIN" -m uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" --limit-concurrency 64
