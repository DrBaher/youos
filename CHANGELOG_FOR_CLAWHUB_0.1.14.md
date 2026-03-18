# YouOS v0.1.14 — ClawHub Metadata Parity Release

## Why this release
Align registry metadata with actual package behavior to remove “instruction-only vs full app” ambiguity and improve install-time trust review.

## Changes

### Registry metadata parity (`clawhub.json`)
- Bumped version to `0.1.14`.
- Added explicit runtime classification:
  - `packageType: application`
  - `execution: local-python`
- Added explicit install workflow metadata:
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `pip install -e .`
- Added explicit credential scope metadata:
  - **Required:** `gog` authentication for Gmail/Docs ingestion
  - **Optional:** Claude/API credentials only when external fallback is enabled

### Skill instructions (`SKILL.md`)
- Added explicit safety line that `pip install -e .` executes local package install code from the repository and should be reviewed before install.

## Intent
- Make package behavior and metadata consistent for security reviewers.
- Preserve local-first defaults while transparently documenting sensitive access and optional network paths.
