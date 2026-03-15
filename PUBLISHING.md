# Publishing YouOS to ClawHub

## Prerequisites
- clawhub CLI installed: `npm install -g clawhub`
- GitHub account authenticated
- All CI checks passing

## Steps
1. Bump version in clawhub.json and CHANGELOG.md
2. Commit: `git commit -m "chore: bump version to X.Y.Z"`
3. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
4. Publish: `clawhub publish ./`
5. Verify on https://clawhub.com/skills/youos

## What clawhub publish does
- Reads SKILL.md and clawhub.json
- Validates the skill structure
- Uploads to the registry
- Makes it installable via `clawhub install youos`
