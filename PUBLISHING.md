# Publishing YouOS to ClawHub

## Prerequisites
- clawhub CLI installed: `npm install -g clawhub`
- GitHub account authenticated
- All CI checks passing

## Steps (default release prep flow)
1. Bump version in `clawhub.json`, `pyproject.toml`, and `CHANGELOG.md`
2. Commit: `git commit -m "chore: bump version to X.Y.Z"`
3. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. Build a **physically clean** release folder:
   - `./scripts/prepare_clawhub_release.sh`
   - Optional custom output: `./scripts/prepare_clawhub_release.sh ~/Documents/youos-release-X.Y.Z`
5. Publish from that folder (not repo root):
   - `cd ~/Documents/youos-release-X.Y.Z`
   - `clawhub publish ./`
6. Verify on https://clawhub.com/skills/youos

## What clawhub publish does
- Reads SKILL.md and clawhub.json
- Validates the skill structure
- Uploads to the registry
- Makes it installable via `clawhub install youos`
