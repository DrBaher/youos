# Capturing the YouOS screenshots

These three assets power the landing page (`site/index.html`, served at
youos.drbaher.com — `pages.yml` bundles this `screenshots/` folder into the
deploy). Re-capture them whenever the UI changes so the site reflects reality.

Capture on a Mac with a trained adapter (so drafts show the real, in-voice
output and the **✍️ your fine-tuned model** badge):

```bash
# 1. Run the server against your real instance
YOUOS_DATA_DIR=~/YouOS-Instances/<you> youos serve     # or: youos service install
```

Set the browser window to **~1280px wide**, system in **dark mode** (the UI is
dark-themed). Capture a clean region with `Cmd-Shift-4` (or `screencapture -i`),
and save each file at the exact name below, then commit — Pages redeploys.

| File | Page | What to show |
|------|------|--------------|
| `demo.gif` | `/feedback` (Draft Reply tab) | A short screen recording: paste a thread → click Generate → the draft **streams in in your voice**. The hero asset — keep it ~5–8s. (Use a recorder like Kap/Gifski.) |
| `01-draft-reply.png` | `/feedback` | A finished draft with the **✍️ your fine-tuned model** badge and the **"How was this generated?"** link visible. |
| `02-stats.png` | `/stats` | The dashboard showing the **System Health → "Drafting with"** row (green = your LoRA) and the **LoRA adapter** status. |

Tips:
- Make sure your voice model is trained + benchmarked first (the readiness banner
  on `/feedback` should be gone) so the screenshots show the real local-model path.
- Keep framing consistent across the three so the landing page looks cohesive.
