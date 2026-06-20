# YouOS Gmail Add-on

Puts YouOS's review experience **inside Gmail** — operate the whole queue without leaving your inbox:

- **The dashboard** — click the YouOS icon from anywhere (no thread open) and the
  homepage card shows your whole queue, scoped by an account switcher: **Drafts to
  review** (Push / Dismiss), **Meeting confirmations** (Approve / Dismiss), **Needs
  review** (Draft it / Dismiss), and **Follow-ups** (owed / awaiting). Every action
  works by id — no need to open the email — and the card refreshes in place. Top-N
  per section, **Prev/Next paged**; a **Refresh** button re-pulls (with a
  last-updated time). Urgent threads sort first with a 🔴 marker; dismiss prompts
  for a reason (with one-tap **Undo**); **inbox-zero** shows a clean empty state.
  This is the in-Gmail equivalent of the `/triage` web page (which stays available
  for desktop).
- **On-demand drafting** — on a thread YouOS hasn't queued (read / hard-skipped /
  brand-new), both the reading card and the compose card offer **"Draft a reply"**
  (optionally steered by a prompt) — it generates in your voice on the spot
  (`/api/agent/draft_for_thread`), so you're never stuck at "no draft".
- **Inline editing** — the contextual draft is an editable field; **Save edits**
  or **Push** (which saves your edits first).
- **Settings** has a **Test connection** button, and error cards offer a one-tap
  **Open Settings**.

- **Reading a thread** — a sidebar card shows YouOS's draft for that thread, its
  calibrated confidence, and the reasons, with near-parity actions to the
  dedicated `/triage` UI:
  - **Push to Gmail Drafts** — create the real draft on the thread, ready to send.
  - **Refine with a prompt** — type a steer ("shorter; decline politely; propose
    Thursday") and **Regenerate** re-drafts in your voice.
  - **Dismiss with feedback** — pick a categorical reason (noise / wrong sender /
    wrong content / already handled / other) + an optional note (the same signal
    the UI captures for tuning).
  - **Mark sent manually** — close a row you handled yourself.
  (Inline draft *editing* stays in the dedicated UI / the compose-insert action
  below — editing long text in a sidebar card is clunky.)
- **Writing a reply** — open the YouOS action while composing and **insert
  YouOS's draft straight into the reply box** (the compose trigger), so you start
  from your own words instead of a blank compose window.

```
Gmail (web/mobile)  ──►  Apps Script add-on  ──►  Tailscale Funnel (HTTPS)  ──►  your local YouOS
        sidebar card        (Google servers)        public URL, PIN+token            REST API
```

The add-on runs on Google's servers, so it can't reach a Tailscale-*private*
instance directly. It calls your YouOS over **Tailscale Funnel** (a public HTTPS
URL), authenticated with a **YouOS API token** (`X-YouOS-Token`). Nothing about
your mail is stored in the add-on — it only renders what YouOS computed locally.

> ⚠️ **This exposes your YouOS instance to the public internet** (PIN + token
> gated). That's a deliberate departure from the default Tailscale-only posture
> (`docs/REMOTE_ACCESS.md`). Do the steps **in order** — a Funnel without a PIN
> is an open inbox engine. The never-send model is unchanged: the add-on can
> only draft / regenerate / dismiss, never send.

## Setup

### 1. Set a PIN first — this turns ON auth
Token auth only enforces when a PIN is set. **Without a PIN, the API is open**, so
this must come before exposing anything.

```sh
youos config set-pin            # or the /settings page
```

### 2. Mint an API token
```sh
youos token-create              # prints the token once — copy it
# revoke later: youos token-revoke <prefix>   (or --all)
```

### 3. Expose YouOS via Tailscale Funnel
Funnel must be enabled for your tailnet (Tailscale admin → Access Controls →
`nodeAttrs`/Funnel). YouOS serves on port **8765** by default.

```sh
tailscale funnel --bg 8765
tailscale funnel status         # shows your public https://<machine>.<tailnet>.ts.net URL
```

Confirm it's reachable and auth is on (401 without the token is the *healthy*
sign):

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://<machine>.<tailnet>.ts.net/api/agent/pending   # expect 401
curl -s -H "X-YouOS-Token: <token>" https://<machine>.<tailnet>.ts.net/api/agent/pending | head # expect JSON
```

### 4. Deploy the add-on (Apps Script)
With [clasp](https://github.com/google/clasp):
```sh
cd integrations/gmail-addon
clasp create --type standalone --title "YouOS"
clasp push                      # uploads Code.gs + appsscript.json
clasp deploy                    # create a test/head deployment
```
Then in the Apps Script editor: **Deploy → Test deployments → Install** (installs
the add-on for your account). Or do it manually: create a new Apps Script
project, paste `Code.gs` and the `appsscript.json` manifest, and install a test
deployment.

### 5. Connect it
Open Gmail → the YouOS icon in the right sidebar → **Settings** → paste your
Funnel **URL** (`https://<machine>.<tailnet>.ts.net`) and the **token** → Save.
Open any conversation YouOS has triaged — the draft, confidence, and reasons
appear, with Regenerate / Dismiss.

## What it uses (server side)
- `GET /api/agent/pending/by_thread/{threadId}` — the add-on's entry point (added
  in b280): YouOS's latest row for the open Gmail thread. Both the read card and
  the compose-insert use it.
- `POST /api/agent/pending/{id}/regenerate` (free-form `instruction` = the
  "refine with a prompt" box) · `POST /api/agent/pending/{id}/dismiss`
  (`reason` + `note`) · `POST /api/agent/pending/{id}/push_to_gmail` ·
  `POST /api/agent/pending/{id}/mark_sent`
- `GET /api/agent/events/by_thread/{threadId}` — a confirmed-meeting card (b282):
  when someone accepts a slot YouOS proposed, the sidebar shows the time +
  attendees with **Approve & create** (creates the Google Calendar event with a
  Meet link + invites) and **Dismiss**.
- `POST /api/agent/events/{id}/approve` · `POST /api/agent/events/{id}/dismiss` —
  approve is gated server-side (send frontier + `agent.calendar.create_events.
  enabled`); a shut gate returns 403 and the card shows why. No new add-on
  scopes: event creation happens on your YouOS host, not in the add-on.

Scopes: `addons.execute`, `addons.current.message.metadata`,
`addons.current.action.compose` (the compose-insert, b281), `script.external_request`.
No broad Gmail read/modify scope — the add-on uses `e.gmail.threadId` and inserts
into the draft you're already editing.

## Security checklist
- **PIN set before Funnel** (step 1) — otherwise the API is open to the internet.
- The token is stored **hashed** on the server and in **per-user** Apps Script
  properties (not in the shared script). Revoke any time with `youos token-revoke`.
- Optionally pin token use to the add-on's origin via `server.token_allowed_origins`.
- Turn the public surface off when you don't need it: `tailscale funnel --bg off 8765`.
