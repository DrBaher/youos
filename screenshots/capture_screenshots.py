"""Capture 4 landing-page screenshots from the live demo instance, dark + light."""
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9999"
VP = {"width": 1340, "height": 900}

INBOUND = ("Before the board call I need a firm answer: can we still commit to the "
           "March 18 migration date, or should I soften it? The customer keeps "
           "pushing for certainty.")
SENDER = "marcus@northwind.io"


def dismiss_banner(pg):
    try:
        b = pg.locator("#modelReadyBanner")
        if b.is_visible():
            pg.locator("#mrbDismiss").click()
            time.sleep(0.3)
    except Exception:
        pass


def cap(pg, path):
    time.sleep(0.6)
    pg.screenshot(path=path)
    print("saved", path)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for theme, suffix in [("dark", ""), ("light", "-light")]:
        ctx = browser.new_context(viewport=VP, device_scale_factor=2)
        ctx.add_init_script("try{localStorage.setItem('youos_tour_done','1');}catch(e){}")
        pg = ctx.new_page()

        # --- 1. Draft tab: generate a real reply ---
        pg.goto(f"{BASE}/feedback?theme={theme}&notour=1")
        pg.wait_for_load_state("networkidle")
        dismiss_banner(pg)
        pg.locator('.mode-btn[data-mode="reply"]').click()
        pg.fill("#sender", SENDER)
        pg.fill("#inbound", INBOUND)
        pg.locator("#btnGenerate").click()
        # wait for the draft textarea to fill
        for _ in range(160):
            v = pg.locator("#draft").input_value()
            if v and len(v.strip()) > 20:
                break
            time.sleep(1)
        time.sleep(1.0)
        cap(pg, f"/tmp/shot-draft{suffix}.png")

        # --- 2. Triage / agent review queue ---
        pg.goto(f"{BASE}/triage?theme={theme}")
        pg.wait_for_load_state("networkidle")
        time.sleep(1.0)
        cap(pg, f"/tmp/shot-triage{suffix}.png")

        # --- 3. Stats ---
        pg.goto(f"{BASE}/stats?theme={theme}")
        pg.wait_for_load_state("networkidle")
        time.sleep(1.2)
        cap(pg, f"/tmp/shot-stats{suffix}.png")

        # --- 4. Facts (tab inside /feedback) ---
        pg.goto(f"{BASE}/feedback?theme={theme}&notour=1")
        pg.wait_for_load_state("networkidle")
        dismiss_banner(pg)
        pg.locator('.tab[data-tab="facts"]').click()
        time.sleep(1.2)
        cap(pg, f"/tmp/shot-facts{suffix}.png")

        ctx.close()
    browser.close()
print("done")
