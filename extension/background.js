// YouOS for Gmail — background service worker.
//
// All calls to the local YouOS server go through here rather than from the
// content script. A content script running on https://mail.google.com would
// hit CORS + mixed-content when calling http://127.0.0.1; the service worker,
// granted host_permissions for localhost, is exempt from page CORS.

const DEFAULT_BASE = "http://127.0.0.1:8765";

async function getConfig() {
  const { youosBaseUrl, youosToken } = await chrome.storage.sync.get(["youosBaseUrl", "youosToken"]);
  return {
    base: (youosBaseUrl || DEFAULT_BASE).replace(/\/+$/, ""),
    token: youosToken || "",
  };
}

async function apiFetch(path, options) {
  const { base, token } = await getConfig();
  const headers = { ...(options.headers || {}) };
  // Authenticate to PIN-protected instances (the SameSite=Lax session cookie
  // can't ride along cross-origin from the extension).
  if (token) headers["X-YouOS-Token"] = token;
  let resp;
  try {
    // redirect:"manual" so an auth redirect to /login surfaces as an
    // opaqueredirect instead of silently following to a non-JSON page.
    resp = await fetch(base + path, { ...options, headers, redirect: "manual" });
  } catch (e) {
    return { ok: false, error: "connect_failed", base, detail: String(e) };
  }

  if (resp.type === "opaqueredirect" || (resp.status >= 300 && resp.status < 400)) {
    return { ok: false, error: "auth_required", base };
  }
  if (resp.status === 429) {
    return { ok: false, error: "rate_limited", base };
  }

  let data = null;
  try {
    data = await resp.json();
  } catch {
    // Non-JSON response (e.g. an HTML error/login page).
    if (!resp.ok) return { ok: false, error: "server_error", status: resp.status, base };
    return { ok: false, error: "bad_response", status: resp.status, base };
  }

  if (!resp.ok) {
    return { ok: false, error: "server_error", status: resp.status, detail: data, base };
  }
  return { ok: true, data };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "youos-generate":
        sendResponse(
          await apiFetch("/feedback/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(msg.body),
          })
        );
        break;
      case "youos-submit":
        sendResponse(
          await apiFetch("/feedback/submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(msg.body),
          })
        );
        break;
      case "youos-config":
        sendResponse(await apiFetch("/api/config", { method: "GET" }));
        break;
      default:
        sendResponse({ ok: false, error: "unknown_message" });
    }
  })();
  return true; // keep the message channel open for the async sendResponse
});

// Toolbar icon toggles the panel in the active Gmail tab.
chrome.action.onClicked.addListener((tab) => {
  if (tab.id != null) {
    chrome.tabs.sendMessage(tab.id, { type: "youos-toggle" }).catch(() => {});
  }
});
