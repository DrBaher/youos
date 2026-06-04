# Capturing the YouOS screenshots

The landing page (`site/index.html`, served at youos.you.com — `pages.yml`
bundles this `screenshots/` folder into the deploy) shows a **2×2 grid** of four
screenshots, each with a light and a dark variant:

| File | Page | Caption / what it shows |
|------|------|--------------|
| `01-draft.png` | `/feedback` | A finished reply written by the local model, with the **✍️ your fine-tuned model** badge, precedent + confidence. |
| `02-triage.png` | `/triage` | The agent triage queue — drafts the agent generated against unread mail, with scores and per-item actions. |
| `03-stats.png` | `/stats` | Corpus health + model status (which model wrote each draft, LoRA adapter state). |
| `04-facts.png` | `/feedback` → Facts tab | Learned facts about contacts, projects, and writing style. |

Each has a `-light.png` sibling (e.g. `01-draft-light.png`). The page swaps them
by theme via CSS keyed on `prefers-color-scheme` *and* `:root[data-theme]` (the
toggle). The dark `01-draft.png` is also the `og:image`.

> `demo.gif` is a separate asset used by the **README** (not the landing grid).

## Reproducible capture (brand-safe, synthetic data)

These shots are captured from a **throwaway demo instance** seeded with synthetic
reply pairs / queue rows / facts — never the real inbox. The brand is set to a
generic "Alex Rivera / AlexOS" in the seed config. Scripts live in this folder:

```bash
REPO=~/YouOS
cd "$REPO"

# 1) Create + seed a demo instance at /tmp/youos_demo (synthetic data only).
YOUOS_DATA_DIR=/tmp/youos_demo .venv/bin/python screenshots/seed_demo_instance.py

# 2) (Optional but recommended) copy a trained LoRA adapter in so the Draft shot
#    shows the "✍️ your fine-tuned model" badge. Output is generated on the
#    SYNTHETIC prompts, so no personal content leaks — it's just style weights.
cp ~/YouOS-Instances/<you>/models/adapters/latest/* /tmp/youos_demo/models/adapters/latest/

# 3) Serve the demo instance. The seed config sets model.server.enabled=true on
#    port 8099, so the first draft spawns a warm LoRA server (loads ~once).
YOUOS_DATA_DIR=/tmp/youos_demo YOUOS_PORT=9999 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9999 &

# 4) Capture all four tabs, light + dark, into /tmp/shot-*.png
.venv/bin/python screenshots/capture_screenshots.py

# 5) The Draft tab's live streaming animation can be flaky on a fresh instance,
#    so capture_draft_shot.py renders the REAL non-stream /draft output (clean
#    LoRA draft + correct model badge) into the live UI and shoots that.
.venv/bin/python screenshots/capture_draft_shot.py

# 6) Promote the captures to the committed asset names, then commit (Pages redeploys).
cd screenshots
for s in draft triage stats facts; do :; done   # see mapping below
cp /tmp/shot-draft.png  01-draft.png   ; cp /tmp/shot-draft-light.png  01-draft-light.png
cp /tmp/shot-triage.png 02-triage.png  ; cp /tmp/shot-triage-light.png 02-triage-light.png
cp /tmp/shot-stats.png  03-stats.png   ; cp /tmp/shot-stats-light.png  03-stats-light.png
cp /tmp/shot-facts.png  04-facts.png   ; cp /tmp/shot-facts-light.png  04-facts-light.png
```

Notes:
- The pages honor `?theme=light|dark` (deep-link override) and `/feedback?notour`
  (suppress the first-run tour) — the capture scripts use both.
- Re-run after any meaningful UI change so the site reflects reality.
- Keep framing at 1340×900 @2× (set in the scripts) so the grid stays cohesive.
