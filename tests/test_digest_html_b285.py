"""Wire-style HTML layout for digest tasks (b285): app/agent/digest_html.py +
the digest_tasks layout='html' path. Pins the placeholder/markdown reject, the
sender-grouped fallback, the layout validation, and that an 'html' inbox digest
sends a body_html alternative.
"""

from __future__ import annotations

import datetime

from app.agent import digest_html
from app.agent import digest_tasks as dt

# --- shared HTML helpers ----------------------------------------------------


def test_validate_rejects_placeholder_and_markdown():
    assert digest_html.validate_sections('<h2>X</h2><ul><li>real</li></ul>')[0]
    assert not digest_html.validate_sections('<h2>X</h2><li>Concrete headline</li>')[0]
    assert not digest_html.validate_sections('<h2>X</h2><li>**bold**</li>')[0]
    assert not digest_html.validate_sections("plain text")[0]


def test_source_name_prefers_display_then_domain():
    assert digest_html._source_name('"vercel[bot]" <notifications@github.com>') == "vercel[bot]"
    assert digest_html._source_name("npm <support@npmjs.com>") == "npm"
    assert digest_html._source_name("plain@example.com") == "example.com"


def test_fallback_groups_by_sender_and_escapes():
    items = [
        {"from": "npm <x@npmjs.com>", "subject": "published a & b", "date": "d1"},
        {"from": "npm <x@npmjs.com>", "subject": "published c", "date": "d2"},
        {"from": "DrBaher <n@github.com>", "subject": "<Run failed>", "date": "d3"},
    ]
    out = digest_html.fallback_sections(items)
    assert out.count("<h2>npm</h2>") == 1            # the two npm items share one card
    assert "&amp;" in out and "&lt;Run failed&gt;" in out
    assert digest_html.validate_sections(out)[0]


def test_build_sections_uses_model_then_falls_back():
    items = [{"from": "npm <x@npmjs.com>", "subject": "published", "date": "d"}]

    def good(p):
        return '<div class="card"><h2>npm</h2><ul><li>1 publish</li></ul></div>'

    assert "1 publish" in digest_html.build_sections(items, instruction="x", complete_fn=good)
    # model output that fails validation → sender-grouped fallback
    out = digest_html.build_sections(items, instruction="x", complete_fn=lambda p: "Concrete headline")
    assert "<h2>npm</h2>" in out
    # no model → fallback
    assert "<h2>npm</h2>" in digest_html.build_sections(items, instruction="x", complete_fn=None)


# --- digest_tasks integration ----------------------------------------------


def test_build_digest_html_wraps_template_with_title(monkeypatch):
    monkeypatch.setattr(dt, "_summary_fn", lambda m: None)   # force the no-model fallback
    items = [{"id": "1", "from": "npm <x@npmjs.com>", "subject": "published", "date": "2026-06-19"}]
    html = dt.build_digest_html(items, name="Dev Digest", complete_fn=None,
                                now=datetime.datetime(2026, 6, 19, 19, 0))
    assert "<!DOCTYPE html>" in html and "Dev Digest" in html
    assert "<h2>npm</h2>" in html


def test_layout_validation():
    base = {"name": "D", "query": "x", "destination": "inbox"}
    assert dt.validate_digest({**base, "layout": "html"})[0]
    assert dt.validate_digest({**base, "layout": "text"})[0]
    assert not dt.validate_digest({**base, "layout": "fancy"})[0]
    # default normalises to text
    assert dt._normalize_digest(base).layout == "text"
    assert dt._normalize_digest({**base, "layout": "html"}).layout == "html"


def test_inbox_html_digest_sends_body_html(monkeypatch, tmp_path):
    # An 'inbox' + layout='html' digest must pass body_html to send_email.
    db = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setattr(dt, "_digest_config",
                        lambda: {"enabled": True, "send_enabled": True, "kill_switch": False})
    monkeypatch.setattr(dt, "_fetch_for_digest",
                        lambda a, q, m: [{"id": "1", "from": "npm <x@npmjs.com>",
                                          "subject": "published", "date": "d"}])
    monkeypatch.setattr(dt, "_undigested", lambda *a, **k: a[-1])
    monkeypatch.setattr(dt, "_claim_period", lambda *a, **k: 1)
    monkeypatch.setattr(dt, "_record_digested", lambda *a, **k: None)
    monkeypatch.setattr(dt, "_update_run", lambda *a, **k: None)
    monkeypatch.setattr(dt, "_period_done", lambda *a, **k: False)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    monkeypatch.setattr(dt, "_summary_fn", lambda m: None)   # deterministic fallback render
    sent = {}

    class _R:
        message_id = "m1"

    def fake_send(**k):
        sent.update(k)
        return _R()

    monkeypatch.setattr("app.ingestion.gmail_write.send_email", fake_send)
    spec = dt._normalize_digest({"name": "Dev Digest", "query": "x", "destination": "inbox",
                                 "layout": "html", "then_archive": False})
    res = dt.run_digest(db, "me@x.com", spec)
    assert res["status"] == "sent"
    assert sent.get("body_html") and "<!DOCTYPE html>" in sent["body_html"]
