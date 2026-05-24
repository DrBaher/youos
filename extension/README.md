# YouOS for Gmail (browser extension)

A Chrome/Edge/Brave (Manifest V3) extension that drafts Gmail replies in your
voice using your **local** YouOS server. It replaces the old bookmarklet, which
was fragile on Gmail (single-line escaping, no reliable way to call a local
HTTP server from an HTTPS page).

Everything stays on your machine — the extension only ever talks to
`http://127.0.0.1:<port>`.

## How it works

```
Gmail page (content.js)  ──message──▶  service worker (background.js)  ──fetch──▶  YouOS API
   extract thread,                       host_permissions: 127.0.0.1            /feedback/generate
   inject panel,                         (bypasses page CORS)                   /feedback/submit
   insert draft                                                                 /api/config
```

The content script can't call `http://127.0.0.1` directly — Gmail is served over
HTTPS and that's a cross-origin request the page's CORS would block. The MV3
**background service worker** has `host_permissions` for localhost, so it makes
the request from the extension context (no CORS, no server changes needed) and
passes the result back to the page.

## Install (unpacked)

1. Start your YouOS server: `youos serve` (defaults to `http://127.0.0.1:8765`).
2. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
3. Enable **Developer mode** (top-right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Open Gmail. A teal ✉ button appears bottom-right — or click the toolbar icon.

If your server runs on a different port, open the extension's **Options** (or
right-click the toolbar icon → Options) and set the URL.

## Usage

1. Open an email in Gmail.
2. Click the ✉ launcher (or the toolbar icon) to open the panel.
3. Sender + message are auto-detected (edit them or click **re-detect** if needed).
4. Optionally add an instruction (e.g. *"decline politely, suggest next Tuesday"*)
   or pick a tone (Shorter / Formal / Detail).
5. Click **Generate draft**. The draft, a confidence badge, and the reasoning appear.
6. Edit inline if you like, then **Insert into Gmail** (drops it at the top of the
   open reply box, keeping your signature) or **Copy**.
7. Rate the draft 1–5 and **Submit feedback** — YouOS learns from it (same
   training signal as the web Review Queue).

## Limitations / notes

- **PIN-protected instances aren't supported yet.** The extension can't ride the
  web UI's session cookie (it's `SameSite=Lax`, cross-origin). If you've set a
  `server.pin`, the extension will report "auth required". A small API-token
  header is the planned fix.
- **Gmail DOM selectors** (`.a3s`, `.gD`, `h2.hP`) are Google's and can change.
  If detection stops working, use **re-detect** or paste the message manually;
  the selectors are isolated in `content.js` for easy updates.
- Chromium only for now. A Firefox build (browser.* shim) is a small follow-up.

## Files

| File | Role |
|------|------|
| `manifest.json` | MV3 manifest (permissions, content script, service worker) |
| `background.js` | Service worker — proxies API calls to the local server |
| `content.js` | Gmail extraction, Shadow-DOM panel, insert-into-compose |
| `options.html` / `options.js` | Set the server URL |
| `icons/` | Toolbar/extension icons |
