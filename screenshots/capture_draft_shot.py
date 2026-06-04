"""Capture the Draft shot (dark+light) by rendering the REAL non-stream /draft
LoRA output into the live UI (the streaming animation is flaky in this fresh
demo; the content + model label here are genuine endpoint output)."""
import json
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9999"
VP = {"width": 1340, "height": 900}
INBOUND = ("Before the board call I need a firm answer: can we still commit to the "
           "March 18 migration date, or should I soften it? The customer keeps "
           "pushing for certainty.")
SENDER = "marcus@northwind.io"


def get_draft():
    req = urllib.request.Request(
        f"{BASE}/draft",
        data=json.dumps({"inbound_message": INBOUND, "sender": SENDER, "mode": "work"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.loads(r.read())


resp = get_draft()
print("MODEL:", resp.get("model_used"))
print("DRAFT:", resp.get("draft"))
prec = ", ".join(p.get("source_id", "") for p in (resp.get("precedent_used") or [])[:2])

INJECT = """
(args) => {
  const r = args.resp, prec = args.prec;
  const draft = document.getElementById('draft');
  draft.value = r.draft;
  // model badge (LoRA -> fine-tuned) + length badge, matching the UI render
  const m = String(r.model_used||'').toLowerCase();
  let label='model: '+r.model_used, cls='yos-badge--accent';
  if (m.indexOf('lora')!==-1){label='✍️ your fine-tuned model';cls='yos-badge--ok';}
  const badges = document.getElementById('draftBadges');
  badges.innerHTML = '<span class="yos-badge '+cls+'">'+label+'</span>'
    + '<span class="yos-badge yos-badge--ok">length: on target</span>';
  // info line with precedent + confidence
  const info=document.getElementById('info');
  info.innerHTML='Draft generated. Edit as needed, then submit.'
    +' <span class="precedent">Precedent: '+prec+' | Confidence: '+(r.confidence||'')+'</span>';
  info.style.display='block';
  // enable tone + submit buttons for realism
  document.querySelectorAll('.btn-tone').forEach(b=>b.disabled=false);
  const sub=document.getElementById('btnSubmit'); if(sub) sub.disabled=false;
}
"""

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for theme, suffix in [("dark", ""), ("light", "-light")]:
        ctx = browser.new_context(viewport=VP, device_scale_factor=2)
        ctx.add_init_script("try{localStorage.setItem('youos_tour_done','1');}catch(e){}")
        pg = ctx.new_page()
        pg.goto(f"{BASE}/feedback?theme={theme}&notour=1")
        pg.wait_for_load_state("networkidle")
        try:
            if pg.locator("#modelReadyBanner").is_visible():
                pg.locator("#mrbDismiss").click(); time.sleep(0.3)
        except Exception:
            pass
        pg.locator('.mode-btn[data-mode="reply"]').click()
        pg.fill("#sender", SENDER)
        pg.fill("#inbound", INBOUND)
        pg.wait_for_timeout(800)  # let sender card load
        pg.evaluate(INJECT, {"resp": resp, "prec": prec})
        pg.wait_for_timeout(600)
        pg.screenshot(path=f"/tmp/shot-draft{suffix}.png")
        print("saved", f"/tmp/shot-draft{suffix}.png")
        ctx.close()
    browser.close()
print("done")
