#!/usr/bin/env bash
set -euo pipefail

# Prepare a physically clean ClawHub release folder.
# Default output: ~/Documents/youos-release-<version>

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f "clawhub.json" ]]; then
  echo "Error: clawhub.json not found in $ROOT_DIR" >&2
  exit 1
fi

VERSION="$(python3 - <<'PY'
import json
from pathlib import Path
j=json.loads(Path('clawhub.json').read_text())
print(j.get('version','').strip())
PY
)"

if [[ -z "$VERSION" ]]; then
  echo "Error: could not read version from clawhub.json" >&2
  exit 1
fi

OUT_DIR="${1:-$HOME/Documents/youos-release-${VERSION}}"

echo "Preparing ClawHub release bundle"
echo "  source : $ROOT_DIR"
echo "  version: $VERSION"
echo "  output : $OUT_DIR"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# Start from repo content, then apply strict excludes.
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'node_modules/' \
  --exclude-from '.clawhubignore' \
  "$ROOT_DIR/" "$OUT_DIR/"

# Extra hygiene: remove common local caches even if they slipped through.
find "$OUT_DIR" -name '.DS_Store' -delete || true
find "$OUT_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + || true
find "$OUT_DIR" -name '*.pyc' -delete || true

# Sanity report
for p in .git .venv .github tests fixtures var instances data .pytest_cache .ruff_cache .herenow gif-frames youos.egg-info; do
  if [[ -e "$OUT_DIR/$p" ]]; then
    echo "WARN: unexpected path still present: $p"
  fi
done

echo "Done. Bundle ready at: $OUT_DIR"
