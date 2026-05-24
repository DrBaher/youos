// YouOS for Gmail — content script.
//
// Injects a launcher button and a Shadow-DOM panel into Gmail. Extracts the
// open thread, asks the local YouOS server (via the background worker) for a
// draft in the user's voice, and inserts the result into the reply box.

(function () {
  "use strict";

  if (window.__youosInjected) return;
  window.__youosInjected = true;

  // Cross-browser: Firefox uses `browser`, Chrome uses `chrome` (both MV3).
  const api = globalThis.browser ?? globalThis.chrome;

  const TONE_LABELS = { shorter: "Shorter", more_formal: "Formal", more_detail: "Detail" };

  let host = null; // shadow host element
  let shadow = null;
  let els = {}; // cached shadow element refs
  let state = { generatedDraft: "", precedents: [], rating: 0 };

  // ── Gmail DOM extraction ──────────────────────────────────────────────

  function extractSubject() {
    const h = document.querySelector("h2.hP, [data-thread-perm-id] h2");
    if (h && h.innerText.trim()) return h.innerText.trim();
    return document.title
      .replace(/\s*-\s*[^-]+@[^-]+\s*-\s*Gmail.*$/, "")
      .replace(/^Gmail\s*-\s*/, "")
      .trim();
  }

  function extractSender() {
    // `.gD` marks the "from" name in each message header and carries the
    // address in its `email` attribute. The last one is the latest message.
    const froms = document.querySelectorAll(".gD[email]");
    if (froms.length) {
      const last = froms[froms.length - 1];
      return last.getAttribute("email") || last.innerText.trim();
    }
    const anyEmail = document.querySelectorAll("span[email]");
    if (anyEmail.length) {
      const last = anyEmail[anyEmail.length - 1];
      return last.getAttribute("email") || last.innerText.trim();
    }
    return "";
  }

  function extractBody() {
    // `.a3s` is the message body container. Prefer the last visible one
    // (the latest expanded message in the open thread).
    const bodies = Array.from(document.querySelectorAll(".a3s"));
    const visible = bodies.filter((el) => el.offsetParent !== null && el.innerText.trim());
    const chosen = visible.length ? visible[visible.length - 1] : bodies[bodies.length - 1];
    return chosen ? chosen.innerText.trim().slice(0, 6000) : "";
  }

  function detectThread() {
    return { subject: extractSubject(), sender: extractSender(), inbound: extractBody() };
  }

  // ── Insert into Gmail compose box ─────────────────────────────────────

  function findComposeBox() {
    return (
      document.querySelector('div[aria-label="Message Body"][contenteditable="true"]') ||
      document.querySelector('.Am.Al.editable[contenteditable="true"]') ||
      document.querySelector('div[role="textbox"][contenteditable="true"]')
    );
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function insertIntoCompose(text) {
    const box = findComposeBox();
    if (!box) return false;
    box.focus();
    // Place the cursor at the very start so an existing signature is kept below.
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(box);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
    const html =
      text
        .split("\n")
        .map((line) => (line ? escapeHtml(line) : "<br>"))
        .join("<br>") + "<br>";
    try {
      document.execCommand("insertHTML", false, html);
      return true;
    } catch {
      box.innerHTML = html + box.innerHTML;
      return true;
    }
  }

  // ── Panel UI (Shadow DOM keeps Gmail's CSS out) ───────────────────────

  const STYLE = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; }
    .fab {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483646;
      width: 52px; height: 52px; border-radius: 50%; border: none; cursor: pointer;
      background: #00c4a7; color: #1a1a2e; font-size: 22px; font-weight: 700;
      box-shadow: 0 4px 14px rgba(0,0,0,0.3);
    }
    .fab:hover { background: #00b398; }
    .panel {
      position: fixed; right: 20px; bottom: 84px; z-index: 2147483647;
      width: 380px; max-height: 80vh; overflow-y: auto;
      background: #1a1a2e; color: #e0e0e0; border: 1px solid #2a3a4a;
      border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,0.4);
      padding: 16px; display: none;
    }
    .panel.open { display: block; }
    .hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
    .hdr .title { color: #00c4a7; font-weight: 700; font-size: 15px; }
    .hdr .x { background: none; border: none; color: #888; font-size: 18px; cursor: pointer; }
    label { display: block; font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .04em; margin: 10px 0 4px; }
    input, textarea {
      width: 100%; background: #0d1b2a; color: #e0e0e0; border: 1px solid #2a3a4a;
      border-radius: 6px; padding: 8px; font-size: 13px; resize: vertical;
    }
    textarea.inbound { min-height: 52px; }
    textarea.draft { min-height: 130px; }
    .tones { display: flex; gap: 6px; margin-top: 6px; }
    .tone { flex: 1; background: #16213e; color: #aaa; border: 1px solid #2a3a4a; border-radius: 6px; padding: 6px; font-size: 12px; cursor: pointer; }
    .tone.active { background: #00c4a7; color: #1a1a2e; border-color: #00c4a7; }
    .btn {
      width: 100%; background: #00c4a7; color: #1a1a2e; border: none; border-radius: 8px;
      padding: 10px; font-size: 13px; font-weight: 700; cursor: pointer; margin-top: 12px;
    }
    .btn:hover { background: #00b398; }
    .btn.secondary { background: #16213e; color: #e0e0e0; border: 1px solid #2a3a4a; }
    .btn:disabled { opacity: .5; cursor: default; }
    .row { display: flex; gap: 8px; }
    .row .btn { margin-top: 8px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge.high { background: #0f3; color: #052; }
    .badge.medium { background: #fd6; color: #530; }
    .badge.low { background: #f66; color: #500; }
    .reason { font-size: 11px; color: #888; margin-top: 6px; line-height: 1.4; }
    .subject { font-size: 11px; color: #7b9; margin-top: 6px; }
    .stars { font-size: 22px; letter-spacing: 2px; margin-top: 4px; }
    .star { cursor: pointer; color: #444; }
    .star.on { color: #fc4; }
    .msg { font-size: 12px; margin-top: 10px; padding: 8px; border-radius: 6px; line-height: 1.4; display: none; }
    .msg.show { display: block; }
    .msg.err { background: #3a1414; color: #f99; border: 1px solid #5a2; border-color: #722; }
    .msg.ok { background: #0d2a1b; color: #9e9; border: 1px solid #275; }
    .hidden { display: none; }
  `;

  const MARKUP = `
    <button class="fab" id="fab" title="YouOS draft">&#x2709;</button>
    <div class="panel" id="panel">
      <div class="hdr"><span class="title">&#x2709;&#xFE0F; YouOS</span><button class="x" id="close">&times;</button></div>

      <label>Replying to</label>
      <input id="sender" placeholder="sender@example.com" />

      <label>Their message <span id="redetect" style="color:#00c4a7;cursor:pointer;text-transform:none;letter-spacing:0;">re-detect</span></label>
      <textarea class="inbound" id="inbound" placeholder="Open an email, or paste the message here."></textarea>

      <label>Instruction (optional)</label>
      <input id="instruction" placeholder="e.g. decline politely, suggest next Tuesday" />
      <div class="tones" id="tones"></div>

      <button class="btn" id="generate">Generate draft</button>

      <div class="msg" id="msg"></div>

      <div id="result" class="hidden">
        <label>Draft <span id="conf"></span></label>
        <textarea class="draft" id="draft"></textarea>
        <div class="subject" id="subject"></div>
        <div class="reason" id="reason"></div>
        <div class="row">
          <button class="btn" id="insert">Insert into Gmail</button>
          <button class="btn secondary" id="copy">Copy</button>
        </div>
        <label>Rate this draft</label>
        <div class="stars" id="stars"></div>
        <button class="btn secondary" id="submit">Submit feedback</button>
      </div>
    </div>
  `;

  function buildPanel() {
    host = document.createElement("div");
    host.id = "youos-host";
    shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    const wrap = document.createElement("div");
    wrap.innerHTML = MARKUP;
    shadow.append(style, wrap);
    document.body.appendChild(host);

    const $ = (id) => shadow.getElementById(id);
    els = {
      fab: $("fab"), panel: $("panel"), close: $("close"),
      sender: $("sender"), inbound: $("inbound"), instruction: $("instruction"),
      tones: $("tones"), generate: $("generate"), msg: $("msg"),
      result: $("result"), draft: $("draft"), conf: $("conf"), subject: $("subject"),
      reason: $("reason"), insert: $("insert"), copy: $("copy"),
      stars: $("stars"), submit: $("submit"), redetect: $("redetect"),
    };

    // Tone buttons
    let activeTone = null;
    Object.entries(TONE_LABELS).forEach(([val, label]) => {
      const b = document.createElement("button");
      b.className = "tone";
      b.textContent = label;
      b.addEventListener("click", () => {
        if (activeTone === val) {
          activeTone = null;
          b.classList.remove("active");
        } else {
          els.tones.querySelectorAll(".tone").forEach((t) => t.classList.remove("active"));
          b.classList.add("active");
          activeTone = val;
        }
      });
      els.tones.appendChild(b);
    });
    els.getTone = () => activeTone;

    // Stars
    for (let i = 1; i <= 5; i++) {
      const s = document.createElement("span");
      s.className = "star";
      s.textContent = "★";
      s.dataset.v = String(i);
      s.addEventListener("click", () => setRating(i));
      els.stars.appendChild(s);
    }

    els.fab.addEventListener("click", togglePanel);
    els.close.addEventListener("click", () => els.panel.classList.remove("open"));
    els.redetect.addEventListener("click", fillFromThread);
    els.generate.addEventListener("click", onGenerate);
    els.insert.addEventListener("click", onInsert);
    els.copy.addEventListener("click", onCopy);
    els.submit.addEventListener("click", onSubmit);
  }

  function setRating(v) {
    state.rating = v;
    els.stars.querySelectorAll(".star").forEach((s) => {
      s.classList.toggle("on", Number(s.dataset.v) <= v);
    });
  }

  function showMsg(text, kind) {
    els.msg.textContent = text;
    els.msg.className = "msg show " + (kind || "");
  }
  function clearMsg() {
    els.msg.className = "msg";
  }

  function fillFromThread() {
    const t = detectThread();
    if (t.sender) els.sender.value = t.sender;
    if (t.inbound) els.inbound.value = t.inbound;
    state.subject = t.subject || "";
  }

  function togglePanel() {
    const open = els.panel.classList.toggle("open");
    if (open && !els.inbound.value) fillFromThread();
  }

  // ── API actions (via background worker) ───────────────────────────────

  function send(message) {
    // Promise form works in both Firefox (browser.*) and Chrome MV3 (chrome.*).
    return api.runtime.sendMessage(message);
  }

  function errorText(res) {
    switch (res.error) {
      case "connect_failed":
        return `Can't reach YouOS at ${res.base}. Is the server running?  (run: youos serve)`;
      case "auth_required":
        return "This YouOS instance is PIN-protected. Run `youos token-create` and paste the token into the extension Options.";
      case "rate_limited":
        return "Rate limit reached (max 10 drafts/min). Wait a moment and retry.";
      case "server_error":
        return `YouOS returned an error (HTTP ${res.status || "?"}). Check the server log.`;
      default:
        return "Unexpected error talking to YouOS.";
    }
  }

  async function onGenerate() {
    const inbound = els.inbound.value.trim();
    if (!inbound) {
      showMsg("No message detected. Open an email or paste text first.", "err");
      return;
    }
    clearMsg();
    els.generate.disabled = true;
    els.generate.textContent = "Generating…";
    const body = {
      inbound_text: inbound,
      sender: els.sender.value.trim() || null,
      mode: "reply",
      user_prompt: els.instruction.value.trim() || null,
      tone_hint: els.getTone(),
    };
    const res = await send({ type: "youos-generate", body });
    els.generate.disabled = false;
    els.generate.textContent = "Generate draft";

    if (!res || !res.ok) {
      showMsg(errorText(res || {}), "err");
      return;
    }
    const d = res.data;
    state.generatedDraft = d.draft || "";
    state.precedents = d.precedent_used || [];
    state.rating = 0;
    setRating(0);

    els.draft.value = state.generatedDraft;
    const c = (d.confidence || "").toLowerCase();
    els.conf.className = "badge " + (["high", "medium", "low"].includes(c) ? c : "");
    els.conf.textContent = c ? c : "";
    els.reason.textContent = d.confidence_reason || "";
    els.subject.textContent = d.suggested_subject ? "Suggested subject: " + d.suggested_subject : "";
    els.result.classList.remove("hidden");
  }

  function onInsert() {
    const text = els.draft.value;
    if (!text.trim()) return;
    if (insertIntoCompose(text)) {
      showMsg("Inserted into the reply box.", "ok");
    } else {
      navigator.clipboard.writeText(text).then(
        () => showMsg("No reply box open — draft copied to clipboard.", "ok"),
        () => showMsg("No reply box open and clipboard blocked. Select the draft and copy manually.", "err")
      );
    }
  }

  function onCopy() {
    navigator.clipboard.writeText(els.draft.value).then(
      () => showMsg("Draft copied to clipboard.", "ok"),
      () => showMsg("Clipboard blocked by the browser.", "err")
    );
  }

  async function onSubmit() {
    if (!state.generatedDraft) {
      showMsg("Generate a draft before submitting feedback.", "err");
      return;
    }
    const body = {
      inbound_text: els.inbound.value.trim(),
      generated_draft: state.generatedDraft,
      edited_reply: els.draft.value.trim() || state.generatedDraft,
      rating: state.rating || null,
      sender: els.sender.value.trim() || null,
      precedents_used: state.precedents,
    };
    els.submit.disabled = true;
    const res = await send({ type: "youos-submit", body });
    els.submit.disabled = false;
    if (res && res.ok) {
      showMsg("Thanks — feedback saved. YouOS learns from this.", "ok");
    } else {
      showMsg(errorText(res || {}), "err");
    }
  }

  // ── Toolbar toggle from the background worker ─────────────────────────
  api.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "youos-toggle") togglePanel();
  });

  // Gmail loads asynchronously; wait for body then inject.
  function init() {
    if (!document.body) {
      setTimeout(init, 300);
      return;
    }
    buildPanel();
  }
  init();
})();
