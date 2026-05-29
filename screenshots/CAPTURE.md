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

## Light + dark variants (theme-aware site)

The landing page swaps screenshots by theme: `01-draft-reply.png` / `02-stats.png`
are the **dark** variants (also used for `og:image`); `01-draft-reply-light.png` /
`02-stats-light.png` are the **light** variants. The page shows the right one via
CSS keyed on `prefers-color-scheme` *and* `:root[data-theme]` (the toggle).

### Re-capturing headlessly (reproducible, brand-safe)

The pages honor `?theme=light|dark` (deep-link override) and `/feedback?notour`
(suppress the first-run tour). They also read the brand from `user.display_name`,
which on a personal instance is `<First>OS` (e.g. BaherOS) — so for **public**
screenshots, temporarily show the generic brand, capture, then restore:

```bash
# 1) point at your instance + serve, then:
B=http://127.0.0.1:8765
curl -s -X POST $B/api/config/identity -d '{"display_name":"YouOS"}'   # generic brand
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
shot(){ "$CHROME" --headless --force-device-scale-factor=2 --virtual-time-budget=4500 \
  --window-size=1300,844 --screenshot="$2" "$1"; }
shot "$B/feedback?theme=dark&notour"  01-draft-reply.png
shot "$B/feedback?theme=light&notour" 01-draft-reply-light.png
shot "$B/stats?theme=dark"            02-stats.png
shot "$B/stats?theme=light"           02-stats-light.png
curl -s -X POST $B/api/config/identity -d '{"display_name":"<First>OS"}' # RESTORE your brand
```

`--virtual-time-budget` lets the async `/api/config` + `/api/stats` calls finish
before the shot. `demo.gif` is a screen recording (Kap/Gifski) and is theme-neutral
enough to serve both modes.
