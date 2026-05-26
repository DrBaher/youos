# Publishing YouOS to ClawHub

## Prerequisites
- clawhub CLI installed: `npm install -g clawhub`
- GitHub account authenticated
- All CI checks passing

## Steps (default release prep flow)
1. Bump version in `clawhub.json`, `pyproject.toml`, and `CHANGELOG.md`
2. Commit: `git commit -m "chore: bump version to X.Y.Z"`
3. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. Build a **minimal allowlist release folder** (default):
   - `./scripts/prepare_clawhub_release.sh`
   - Optional custom output: `./scripts/prepare_clawhub_release.sh ~/Documents/youos-release-X.Y.Z`
   - This script includes only: `app/`, `clawhub.json`, `configs/`, `PRIVACY.md`, `pyproject.toml`, `README.md`, `scripts/`, `SKILL.md`
   - **Text-only**: ClawHub rejects binary files, so `screenshots/` (resolved from the homepage repo) and `extension/` (ships PNG icons) are excluded; the script aborts if any binary slips in
5. Upload via the **dashboard** at https://clawhub.ai/dashboard (drag the release folder
   or a zip of it). This is the reliable path.
   - The `clawhub publish ./` CLI currently times out (~49s, stuck in "Preparing") regardless
     of pack size/content — a registry-side issue, not your bundle. Use the dashboard instead.
   - The dashboard accepts the semver form of the version (e.g. `0.2.0-beta.14`); the repo's
     `0.2.0b14` is the PEP 440 equivalent.
6. Verify on https://clawhub.ai/skills/youos

## What clawhub publish does
- Reads SKILL.md and clawhub.json
- Validates the skill structure
- Uploads to the registry
- Makes it installable via `clawhub install youos`
