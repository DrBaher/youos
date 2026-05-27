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
    :host { all: initial;
      --teal:#00c4a7; --teal-dim:#00b398; --bg:#1a1a2e; --surface:#16213e; --surface-2:#0d1b2a;
      --border:#2a3a4a; --text:#e0e0e0; --muted:#aaa; --muted-2:#888; --faint:#444;
      --warn:#f0ad4e; --success:#2ecc71; --on-accent:#1a1a2e; }
    @media (prefers-color-scheme: light) { :host(:not([data-theme="dark"])) {
      --teal:#0a8f7c; --teal-dim:#0a8f7c; --bg:#ffffff; --surface:#f4f6f9; --surface-2:#eef1f5;
      --border:#d8dee6; --text:#1a2230; --muted:#5b6675; --muted-2:#6b7585; --faint:#c2c9d2;
      --warn:#b7791f; --success:#1a8a4f; --on-accent:#ffffff; } }
    :host([data-theme="light"]) {
      --teal:#0a8f7c; --teal-dim:#0a8f7c; --bg:#ffffff; --surface:#f4f6f9; --surface-2:#eef1f5;
      --border:#d8dee6; --text:#1a2230; --muted:#5b6675; --muted-2:#6b7585; --faint:#c2c9d2;
      --warn:#b7791f; --success:#1a8a4f; --on-accent:#ffffff; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; }
    .fab {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483646;
      width: 52px; height: 52px; border-radius: 50%; border: none; cursor: pointer;
      background: var(--teal); color: var(--on-accent); font-size: 22px; font-weight: 700;
      box-shadow: 0 4px 14px rgba(0,0,0,0.3);
    }
    .fab:hover { background: var(--teal-dim); }
    .panel {
      position: fixed; right: 20px; bottom: 84px; z-index: 2147483647;
      width: 380px; max-height: 80vh; overflow-y: auto;
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,0.4);
      padding: 16px; display: none;
    }
    .panel.open { display: block; }
    .hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
    .hdr .title { color: var(--teal); font-weight: 700; font-size: 15px; }
    .hdr .x { background: none; border: none; color: var(--muted-2); font-size: 18px; cursor: pointer; }
    label { display: block; font-size: 11px; color: var(--muted-2); text-transform: uppercase; letter-spacing: .04em; margin: 10px 0 4px; }
    input, textarea {
      width: 100%; background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
      border-radius: 6px; padding: 8px; font-size: 13px; resize: vertical;
    }
    textarea.inbound { min-height: 170px; }
    textarea.draft { min-height: 130px; }
    .tones { display: flex; gap: 6px; margin-top: 6px; }
    .tone { flex: 1; background: var(--surface); color: var(--muted); border: 1px solid var(--border); border-radius: 6px; padding: 6px; font-size: 12px; cursor: pointer; }
    .tone.active { background: var(--teal); color: var(--on-accent); border-color: var(--teal); }
    .btn {
      width: 100%; background: var(--teal); color: var(--on-accent); border: none; border-radius: 8px;
      padding: 10px; font-size: 13px; font-weight: 700; cursor: pointer; margin-top: 12px;
    }
    .btn:hover { background: var(--teal-dim); }
    .btn.secondary { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
    .btn:disabled { opacity: .5; cursor: default; }
    .row { display: flex; gap: 8px; }
    .row .btn { margin-top: 8px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .badge.high { background: #0f3; color: #052; }
    .badge.medium { background: #fd6; color: #530; }
    .badge.low { background: #f66; color: #500; }
    .reason { font-size: 11px; color: var(--muted-2); margin-top: 6px; line-height: 1.4; }
    .subject { font-size: 11px; color: #7b9; margin-top: 6px; }
    .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .mbadge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; border: 1px solid var(--border); color: var(--muted); }
    .mbadge.ok { color: var(--success); border-color: var(--success); }
    .mbadge.warn { color: var(--warn); border-color: var(--warn); }
    .mbadge.accent { color: var(--teal); border-color: var(--teal); }
    .cands { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
    .cands .chead { font-size: 11px; color: var(--muted-2); }
    .cand { text-align: left; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 8px; color: var(--text); cursor: pointer; font-size: 12px; }
    .cand:hover { border-color: var(--teal-dim); }
    .cand.sel { border-color: var(--teal); background: rgba(0,196,167,0.08); }
    .cand .cm { font-size: 10px; color: var(--muted-2); margin-bottom: 4px; }
    .cand .cp { color: var(--muted); line-height: 1.4; white-space: pre-wrap; }
    .stars { font-size: 22px; letter-spacing: 2px; margin-top: 4px; }
    .star { cursor: pointer; color: var(--faint); }
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
      <div class="hdr"><span class="title">&#x2709;&#xFE0F; YouOS</span><span class="actions" style="display:flex;gap:4px;align-items:center;"><button class="thm" id="theme" title="Toggle theme" style="background:none;border:none;color:var(--muted-2);font-size:15px;cursor:pointer;line-height:1;">&#x263E;</button><button class="x" id="close">&times;</button></span></div>

      <label>Replying to</label>
      <input id="sender" placeholder="sender@example.com" />

      <label>Their message <span id="redetect" style="color:var(--teal);cursor:pointer;text-transform:none;letter-spacing:0;">re-detect</span></label>
      <textarea class="inbound" id="inbound" placeholder="Open an email, or paste the message here."></textarea>

      <label>Instruction (optional)</label>
      <input id="instruction" placeholder="e.g. decline politely, suggest next Tuesday" />
      <div class="tones" id="tones"></div>

      <button class="btn" id="generate">Generate draft</button>

      <div class="msg" id="msg"></div>

      <div id="result" class="hidden">
        <label>Draft <span id="conf"></span></label>
        <textarea class="draft" id="draft"></textarea>
        <div class="meta" id="meta"></div>
        <div class="cands" id="candidates"></div>
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
    try { var _th = localStorage.getItem("youos-theme"); if (_th === "light" || _th === "dark") host.setAttribute("data-theme", _th); } catch (e) {}

    const $ = (id) => shadow.getElementById(id);
    els = {
      fab: $("fab"), panel: $("panel"), close: $("close"), theme: $("theme"),
      sender: $("sender"), inbound: $("inbound"), instruction: $("instruction"),
      tones: $("tones"), generate: $("generate"), msg: $("msg"),
      result: $("result"), draft: $("draft"), conf: $("conf"), subject: $("subject"),
      reason: $("reason"), insert: $("insert"), copy: $("copy"),
      stars: $("stars"), submit: $("submit"), redetect: $("redetect"),
      meta: $("meta"), candidates: $("candidates"),
    };

    function _effTheme() { var a = host.getAttribute("data-theme"); return a || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); }
    function _themeIcon() { els.theme.textContent = _effTheme() === "dark" ? "\u2600" : "\u263E"; }
    _themeIcon();
    els.theme.addEventListener("click", function () {
      var n = _effTheme() === "dark" ? "light" : "dark";
      host.setAttribute("data-theme", n);
      try { localStorage.setItem("youos-theme", n); } catch (e) {}
      _themeIcon();
    });

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
    els.meta.innerHTML = "";
    els.candidates.innerHTML = "";
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
    renderMeta(d);
    els.result.classList.remove("hidden");
  }

  // Surface the draft-quality fields the web UI also shows: length flag,
  // post-generation repairs, and the multi-candidate picker.
  function renderMeta(d) {
    els.meta.innerHTML = "";
    els.candidates.innerHTML = "";

    if (d.length_flag) {
      const b = document.createElement("span");
      b.className = "mbadge " + (d.length_flag === "ok" ? "ok" : "warn");
      b.textContent = d.length_flag === "ok" ? "length: on target" : "length: " + d.length_flag;
      els.meta.appendChild(b);
    }
    if (Array.isArray(d.repairs) && d.repairs.length) {
      const b = document.createElement("span");
      b.className = "mbadge accent";
      b.title = "post-generation repairs applied";
      b.textContent = "repaired: " + d.repairs.map((r) => r.replace(/_/g, " ")).join(", ");
      els.meta.appendChild(b);
    }

    const cands = Array.isArray(d.candidates) ? d.candidates : [];
    if (cands.length > 1) {
      const head = document.createElement("div");
      head.className = "chead";
      head.textContent = cands.length + " candidates — click to use one:";
      els.candidates.appendChild(head);
      cands.forEach((c, i) => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "cand" + (i === 0 ? " sel" : "");
        const meta = document.createElement("div");
        meta.className = "cm";
        const bits = ["#" + (i + 1)];
        if (i === 0) bits.push("best");
        if (c.temperature != null) bits.push("temp " + c.temperature);
        if (c.score != null && isFinite(c.score)) bits.push("score " + Number(c.score).toFixed(2));
        meta.textContent = bits.join(" · ");
        const prev = document.createElement("div");
        prev.className = "cp";
        prev.textContent = String(c.draft || "").slice(0, 200);
        card.append(meta, prev);
        card.addEventListener("click", () => {
          els.draft.value = c.draft || "";
          state.generatedDraft = c.draft || "";
          els.candidates.querySelectorAll(".cand").forEach((x) => x.classList.remove("sel"));
          card.classList.add("sel");
        });
        els.candidates.appendChild(card);
      });
    }
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
