"""Browser UI smoke test (Playwright) against a disposable, seeded instance.

This is the regression coverage the plain-HTML/JS UI never had: it boots a real
YouOS server on a temp data dir, seeds synthetic data (never touches a real
instance), drives every page with headless Chromium, and asserts there are NO
uncaught JS exceptions / console errors / failed requests — the class of bug that
static review and even screenshots miss. Plus a few model-free interactions
(triage cards render, rules form works, a settings toggle persists).

OPT-IN: skipped unless ``YOUOS_UI_TESTS=1`` and Playwright + its Chromium are
installed, so the default fast CI (which has neither) stays green. To run:

    pip install playwright && playwright install chromium
    YOUOS_UI_TESTS=1 pytest tests/test_ui_smoke.py -v

Model-dependent draft *generation* is intentionally NOT asserted (slow,
nondeterministic) — only that the pages and their structure are healthy.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if not os.environ.get("YOUOS_UI_TESTS"):
    pytest.skip("UI smoke tests are opt-in: set YOUOS_UI_TESTS=1", allow_module_level=True)

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

PAGES = ["feedback", "stats", "settings", "welcome", "triage", "rules", "digests", "about", "draft-popup"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(db_path: Path) -> None:
    """Synthetic unreviewed reply_pairs + pending triage drafts (status='pending')."""
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=2)).isoformat()
    con = sqlite3.connect(db_path)
    pairs = [
        ("alice@acmecorp.com",
         "Hi, can you confirm the Q3 delivery timeline before the board meeting next week?",
         "Sure — proposal attached, timeline holds for Q3."),
        ("dave@gmail.com",
         "Hey, are we still on for dinner this Saturday? Let me know what time works for you.",
         "Yes! Let's do 7:30pm, I'll text you the address."),
    ]
    for i, (author, inb, rep) in enumerate(pairs):
        con.execute(
            "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, inbound_author, paired_at, metadata_json, auto_feedback_processed) "
            "VALUES ('gmail',?,?,?,?,?,'{}',0)",
            (f"uismoke-{i}", inb, rep, author, recent),
        )
    con.execute(
        """INSERT INTO agent_pending_drafts (message_id, thread_id, account, sender, sender_email, subject, body, received_at,
               needs_reply_score, tier, urgency_score, draft, draft_model, status, created_at, quality_score, send_state)
           VALUES ('sm1','t1','test@local','Client Corp','pm@clientcorp.com','Export regression blocking demo',
               'CSV downloads are empty in the latest build.', ?, 0.91, 'draft', 0.91,
               'Reproduced it — fix is in review, shipping within the hour.', 'qwen3-4b-lora', 'pending', ?, 0.8, 'none')""",
        (recent, recent),
    )
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory):
    inst = tmp_path_factory.mktemp("youos_ui")
    (inst / "var").mkdir()
    (inst / "configs").mkdir()
    (inst / "docs").mkdir()
    (inst / "docs" / "schema.sql").write_text((ROOT / "docs" / "schema.sql").read_text())
    (inst / "youos_config.yaml").write_text((ROOT / "youos_config.yaml").read_text())

    env = {**os.environ, "YOUOS_DATA_DIR": str(inst)}
    # Bootstrap the schema, then seed.
    subprocess.run(
        [sys.executable, "-c", "from app.db.bootstrap import bootstrap_database; bootstrap_database()"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )
    _seed(inst / "var" / "youos.db")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env={**env, "YOUOS_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        import urllib.request
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{proc.stdout.read().decode()[-2000:]}")
            try:
                with urllib.request.urlopen(f"{base}/feedback", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not become healthy in time")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _instrument(page):
    sig = {"console": [], "pageerror": [], "bad": []}
    page.on("console", lambda m: sig["console"].append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: sig["pageerror"].append(str(e)))
    page.on("response", lambda r: sig["bad"].append(f"{r.status} {r.url}") if r.status >= 500 else None)
    return sig


@pytest.mark.parametrize("name", PAGES)
def test_page_has_no_js_errors(ui_server, browser, name):
    """Every page loads clean: no uncaught JS, no console errors, no 5xx."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    sig = _instrument(page)
    try:
        page.goto(f"{ui_server}/{name}", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        assert "YouOS" in page.title() or page.title()  # sanity: a title rendered
        assert sig["pageerror"] == [], f"{name}: uncaught JS exceptions: {sig['pageerror']}"
        assert sig["console"] == [], f"{name}: console errors: {sig['console']}"
        assert sig["bad"] == [], f"{name}: 5xx responses: {sig['bad']}"
    finally:
        page.close()


def test_triage_renders_seeded_draft(ui_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        page.goto(f"{ui_server}/triage", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        assert page.get_by_text("Export regression blocking demo").count() >= 1
    finally:
        page.close()


def test_review_queue_preview_renders(ui_server, browser):
    """Opening Review Queue streams the inbound preview (model-free part)."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        page.goto(f"{ui_server}/feedback", wait_until="networkidle", timeout=30000)
        skip = page.get_by_role("button", name="Skip")
        if skip.count() and skip.first.is_visible():
            skip.first.click()
        page.get_by_text("Review Queue", exact=False).first.click()
        # The inbound preview arrives before any model draft.
        page.wait_for_selector("text=board meeting", timeout=15000)
    finally:
        page.close()


def test_settings_toggle_persists(ui_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        page.goto(f"{ui_server}/settings", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)
        box = page.locator("#flags label.switch input[type=checkbox]").first
        before = box.is_checked()
        page.locator("#flags label.switch").first.click()
        page.wait_for_timeout(1200)  # auto-saves to the instance config
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)
        after = page.locator("#flags label.switch input[type=checkbox]").first.is_checked()
        assert after == (not before), "settings toggle did not persist across reload"
    finally:
        page.close()
