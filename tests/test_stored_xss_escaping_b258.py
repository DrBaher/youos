"""b258: attacker-derived fields are HTML-escaped before innerHTML.

Pass-10 XSS re-audit found 5 raw-innerHTML sinks (2 HIGH stored XSS): a
crafted From display name like '<img src=x onerror=...>' flows from the
inbound header into sender_profiles.display_name and was rendered raw in the
/stats top-senders table and the feedback sender card. These pin the
escaping at the source so the consistency failure can't silently return.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _stats() -> str:
    return client.get("/stats").text


def _feedback() -> str:
    return client.get("/feedback").text


def test_stats_top_senders_escapes_display_name_and_company():
    html = _stats()
    # the table row must build its sender cells through YouOS.esc
    assert "YouOS.esc(s.display_name" in html
    assert "YouOS.esc(s.company" in html
    assert "YouOS.esc(s.sender_type" in html
    # and the raw interpolation must be gone
    assert "'<td>' + (s.display_name" not in html


def test_feedback_sender_card_escapes_profile_fields():
    html = _feedback()
    assert "YouOS.esc(p.display_name" in html
    assert "YouOS.esc(p.company)" in html
    assert "YouOS.esc(noteText)" in html
    assert "<span class=\"sc-name\">' + (p.display_name" not in html  # raw form gone


def test_feedback_exemplar_subject_escaped():
    html = _feedback()
    assert "YouOS.esc(ex.subject" in html
    assert "'. ' + (ex.subject" not in html


def test_feedback_facts_toast_and_tags_escaped():
    html = _feedback()
    assert "YouOS.esc(f.fact)" in html
    assert "escHtml(t)" in html  # fact tags


def test_feedback_eschtml_escapes_single_quote():
    """The local escHtml helper was missing the single-quote replacement."""
    html = _feedback()
    # the function body must now include the apostrophe rule
    m = re.search(r"function escHtml\(s\)\s*\{(.+?)\}", html, re.DOTALL)
    assert m, "escHtml not found"
    assert "&#39;" in m.group(1)


def test_youos_esc_covers_all_five_chars():
    js = client.get("/static/youos.js").text
    body = js.split("YouOS.esc =", 1)[1].split("};", 1)[0]
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in body
