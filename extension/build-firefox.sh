#!/usr/bin/env bash
# Assemble a Firefox-loadable build from the shared extension sources.
# The JS/HTML/icons are identical to the Chrome version; only the manifest
# differs (Firefox MV3 uses background.scripts + browser_specific_settings).
set -euo pipefail
cd "$(dirname "$0")"

OUT="firefox-build"
rm -rf "$OUT"
mkdir -p "$OUT/icons"

cp background.js content.js options.js options.html "$OUT/"
cp icons/icon16.png icons/icon48.png icons/icon128.png "$OUT/icons/"
cp manifest.firefox.json "$OUT/manifest.json"

echo "Firefox build ready in extension/$OUT/"
echo "Load it via: about:debugging#/runtime/this-firefox → Load Temporary Add-on → pick $OUT/manifest.json"
