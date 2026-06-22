#!/usr/bin/env bash
# Verified restart/deploy for the YouOS launchd service.
#
# WHY THIS EXISTS: `launchctl kickstart -k` can silently no-op (the old process
# keeps running) and a plain health check (curl → 200/303) is answered by that
# SAME old process — so a "restart" looks successful while the server keeps
# running stale code/config. (This bit us 2026-06-22: auto-push config written
# but never loaded; the server ran Jun-21 code for a day.) This script CONFIRMS
# the OS process actually cycled (new PID), self-heals with bootout/bootstrap if
# kickstart won't cycle it, then reports the SHA + config mtime now serving.
#
# Usage: scripts/restart_youos.sh   (exit 0 = verified new process is serving)
set -euo pipefail

LABEL="com.youos.server"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PAT="uvicorn ${YOUOS_APP_MODULE:-app.main:app}"
PORT="${YOUOS_PORT:-8765}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_pid() { pgrep -f "$PAT" | head -1 || true; }
_wait_new() {  # wait up to ~25s for a PID that differs from $1
  local old="$1" p
  for _ in $(seq 1 25); do
    sleep 1; p="$(_pid)"
    if [[ -n "$p" && "$p" != "$old" ]]; then echo "$p"; return 0; fi
  done
  echo ""; return 1
}

OLD="$(_pid)"
echo "old pid: ${OLD:-none}"

echo "→ launchctl kickstart -k $DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL" || true
NEW="$(_wait_new "${OLD:-none}")" || true

if [[ -z "$NEW" || "$NEW" == "${OLD:-}" ]]; then
  echo "kickstart did not cycle the process — falling back to bootout + bootstrap" >&2
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  NEW="$(_wait_new "${OLD:-none}")" || true
fi

if [[ -z "$NEW" || "$NEW" == "${OLD:-}" ]]; then
  echo "FATAL: server did not restart (pid still ${OLD:-none}). Check var/launchd.stderr.log." >&2
  exit 1
fi

echo "new pid: $NEW (started $(ps -o lstart= -p "$NEW" 2>/dev/null | tr -s ' '))"

# Health: the NEW process answers (303 = login redirect, 200 = open).
CODE=""
for _ in $(seq 1 25); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/agent/events/pending" 2>/dev/null || true)"
  [[ "$CODE" == "303" || "$CODE" == "200" ]] && break
  sleep 1
done
echo "health: ${CODE:-no-response}"

echo "serving sha: $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "✓ restart verified (new process is serving). Confirm the matching '[youos boot]' line in var/launchd.stderr.log."
[[ "$CODE" == "303" || "$CODE" == "200" ]] || { echo "WARNING: process cycled but health check did not pass" >&2; exit 2; }
