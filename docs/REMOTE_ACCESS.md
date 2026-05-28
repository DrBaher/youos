# Remote access — YouOS from your phone or another machine

YouOS runs locally on Apple Silicon by design — the model, the LoRA, your inbox corpus, your draft history all stay on your Mac. But the *UI* (`/triage`, `/feedback`, `/stats`, `/settings`) is just a FastAPI server; it can be reached from any device with a network route to your Mac.

This doc covers the **Tailscale + PIN** path, which is the recommended remote-access setup. It gives you a private network between your Mac and your phone (and any other devices on your Tailnet) without exposing anything to the public internet.

## What you can do remotely

| Action | Where it lives |
|---|---|
| Read drafts the agent queued | Gmail.com (after `Push to Gmail Drafts`) |
| Finish and send a drafted reply | Gmail.com |
| Browse the `/triage` queue | YouOS server (this doc) |
| Dismiss with a categorical reason | YouOS server |
| Save as training pair (edit + capture for LoRA retrain) | YouOS server |
| Check `Agent health` (sweep stats, dismissal rate, score histogram) | YouOS server |
| Toggle flags at `/settings` | YouOS server |

The Gmail path works on any device without additional setup. The YouOS server requires a network route, which Tailscale provides.

## Prerequisites

- [Tailscale](https://tailscale.com/download) installed on **your Mac** (it should already be — `tailscale status` should print your devices) and on **your phone** (App Store / Play Store).
- Both devices logged into the same Tailnet account.

You don't need a paid plan — the free tier covers everything below.

## Setup (one-time, ~2 minutes)

### 1. Find your Mac's Tailscale hostname

```bash
tailscale status | head -1
```

The first column is the IP, the second is the hostname. Use the hostname (e.g. `bbots-mac-mini`) — it's stable across reboots.

### 2. Set a PIN (required — protects the exposed UI)

```bash
# Replace `YOUR-PIN-HERE` with a real PIN; never commit it.
youos config set server.pin YOUR-PIN-HERE
```

PIN can be any string — 6+ characters recommended. You'll be prompted for it once per device (then it's cookied).

> **Don't skip this.** Once you bind the server to a non-loopback interface, anyone on your Tailnet can reach it. The PIN is what limits access to you.

### 3. Bind YouOS to the Tailscale interface

```bash
youos config set server.host 0.0.0.0
youos config set tailscale.hostname bbots-mac-mini   # use your hostname from step 1
```

`0.0.0.0` binds to all interfaces on your Mac. Tailscale routes Tailnet traffic to it; localhost still works from the Mac itself; LAN devices that aren't on your Tailnet are still blocked by your local firewall (assuming default macOS settings).

If you want stricter binding (only the Tailscale interface, not LAN at all), use your Tailscale IP from step 1: `youos config set server.host 100.79.48.17`. The trade-off is that your IP changes if Tailscale reassigns it (rare on free tier).

### 4. Restart the server

```bash
pkill -f "youos serve"
youos serve &
youos status
```

`youos status` should now print:

```
Tailscale:   ✅ http://bbots-mac-mini:8901
```

### 5. Open `/triage` on your phone

On your phone (with Tailscale running and connected to the same Tailnet):

```
http://bbots-mac-mini:8901/triage
```

Or use the IP form (`http://100.79.48.17:8901/triage`) if MagicDNS isn't resolving on your phone.

First visit prompts for the PIN. After that, it's cookied for ~30 days.

### 6. (Optional) Add to your phone's home screen

In Safari: tap the Share button → "Add to Home Screen". Names it whatever you want. From your home screen it opens like an app.

## What's protected

- **PIN gates browser access** to all routes except `/login` and static assets.
- **API tokens** (for the Gmail extension's cross-origin calls) are a separate path; see `/settings → Auth` to manage.
- **Tailscale provides network-level identity** — only devices logged into your Tailnet can reach the bind.

## What's not yet supported (gaps to know about)

- **Push notifications to your phone** — the agent loop only fires `display notification` on the Mac. No iOS/Android push integration. **Workaround**: pipe the daily digest (below) to your phone via email.

## Remote dismissal via Gmail label

If `/triage` isn't reachable but Gmail is (the universal case — phone, friend's laptop, work web client), you can still dismiss a queued draft by applying a Gmail label to the original thread.

### Setup (once)

1. In Gmail web (or any client), create a label called **`YouOS/skip`** (the slash creates a nested label under a YouOS folder for tidiness).
2. That's it — `run_triage` checks for this label at the start of every sweep.

### Usage

When you see one of the agent's drafts in **Gmail Drafts** that shouldn't have been drafted:

1. Open the **original inbound thread** the draft replies to (not the Draft itself).
2. Apply the **`YouOS/skip`** label (sidebar → Labels → YouOS/skip).
3. Next sweep (within `agent.interval_minutes`), the matching `agent_pending_drafts` row is marked **dismissed with reason='noise'**, and the label is removed from the thread so it isn't reprocessed.

You can also run the sync immediately:

```bash
youos sync-labels                              # default account, default label
youos sync-labels --account other@x.com       # specific account
youos sync-labels --label "Custom/skip-tag"   # custom label name
```

The dismissal goes through the same path as `/triage`'s dismiss button, so it counts in the dismissal-feedback aggregate, contributes to `agent.auto_promote_skip_senders` once you hit 3+ dismissals from the same sender, and shows up on the next digest email.

### What if I label something not in the queue?

Safe — `sync-labels` skips threads with no matching `agent_pending_drafts` row (e.g. an old thread you labelled before the agent saw it). The label stays applied; nothing happens.

## Multi-account setup

YouOS handles multiple Gmail accounts on one machine. Each account runs through gog's stored OAuth tokens; the agent sweeps them sequentially on each scheduler tick.

### Setup

1. Authorize each account with gog (one-time per account):

   ```bash
   gog auth login --account drbaher@gmail.com
   gog auth login --account baher@medicus.ai
   gog auth list                       # confirm both are listed
   ```

2. Add both to `user.emails`:

   ```bash
   youos config set user.emails 'drbaher@gmail.com, baher@medicus.ai'
   ```

3. The scheduler now sweeps both accounts each tick (no agent restart needed — `get_agent_config` re-reads on every iteration).

### What's per-account vs global

| Item | Per-account | Global |
|---|---|---|
| `agent_pending_drafts` rows (the queue at `/triage`) | ✓ | |
| `agent_audit` rows (sweep history) | ✓ | |
| `/api/agent/observability?account=X` | ✓ | |
| Dismissal stats / candidates / promotion list | ✓ | |
| `agent.enabled` | | ✓ |
| `agent.interval_minutes` | | ✓ |
| `agent.standing_instructions` | | ✓ |
| `agent.skip_senders` | | ✓ (mute applies to both inboxes) |
| `agent.daily_draft_cap` | | ✓ (per-UTC-day quota applies per account) |
| `agent.threshold` / `window` / `limit` | | ✓ |
| `agent.auto_promote_skip_senders` | | ✓ |
| Standing instructions snapshot (column on each row) | ✓ (snapshotted at draft time) | |

If you want different standing-instructions or skip-senders per account, that's a deliberate future feature — current design is "configure once, applies to both."

### Selecting which accounts the scheduler sweeps

By default the scheduler picks up everything in `user.emails`. To restrict (e.g. pause one account temporarily without losing its identity entry):

```bash
youos config set agent.accounts 'drbaher@gmail.com'   # sweep only drbaher
youos config set agent.accounts ''                    # back to user.emails
```

### Verifying multi-account sweeps

The `/triage` Recent activity table shows the `Account` column for each sweep. The digest CLI is per-account too:

```bash
youos digest --account drbaher@gmail.com
youos digest --account baher@medicus.ai
```

On a typical inbox, one account-sweep takes 20–45 seconds. With two accounts and a 1-minute interval, expect the loop to be busy most of the time and finish each tick around the 60s mark. Set `agent.interval_minutes` higher (5, 15) for less churn.

## Daily digest email (poor-man's push notification)

`youos digest` prints a summary of the agent's recent activity — sweep counts, drafts pending vs pushed, dismissal rate by reason, auto-promoted senders, top noise-dismissed senders, and a clickable Tailscale link to `/triage`. Pipe it to `mail` via cron and you get a daily email digest of what the agent did while you were away.

```bash
youos digest                      # today, plain text
youos digest --days 7             # last week
youos digest --format html        # HTML for richer email rendering
youos digest --format json | jq . # structured for further processing
```

**Cron recipe** (sends an HTML digest every weekday at 7am):

```cron
# crontab -e
0 7 * * 1-5  cd ~/YouOS && ~/YouOS/.venv/bin/youos digest --format html | mail -s "$(date '+YouOS daily — %a %b %d')" -a 'Content-Type: text/html; charset=utf-8' you@yourmail.com
```

The `-a 'Content-Type: text/html'` flag is what makes `mail` send HTML instead of escaping it. Tested on macOS `mail` (which uses `/usr/sbin/sendmail` under the hood).

If you don't have `mail` configured, the digest also works fine piped to `pbcopy` (clipboard) or saved to a file you sync to your phone via iCloud Drive.

## Troubleshooting

**`youos status` doesn't show Tailscale URL.** Check `tailscale.hostname` is set (`youos config get tailscale.hostname`) and that `tailscale status` lists your Mac.

**Phone can resolve but can't connect.** Confirm `server.host` is `0.0.0.0` (or your Tailscale IP), not `127.0.0.1`. Restart the server after changing.

**Phone resolves the hostname but is told `connection refused`.** Server isn't running, or it's bound to loopback only. Check `pgrep -af "uvicorn.*app.main:app"`.

**PIN prompt loops.** Cookie storage may be disabled on the phone browser. Try a different browser or check the browser's site-settings → cookies.

**Tailscale shows the Mac as `offline`.** Make sure Tailscale is actually running on the Mac (`brew services start tailscale` or the menubar icon). Tailscale sometimes pauses on macOS sleep — wake the Mac.
