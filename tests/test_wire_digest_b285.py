"""The Wire newsletter digest (b285): native port of the OpenClaw skill.

Pins the parts that protect the user: the never-send gating, the empty-day
no-op, per-message dedup, edition continuity/bump-only-on-send, the
skip/promo/archive-exclusion filters, the weekdays-only schedule, and the
placeholder/markdown reject that stops a half-rendered digest going out.
"""

from __future__ import annotations

import datetime

from app.agent import wire_digest as w

# --- filters ----------------------------------------------------------------


def test_skip_from_and_subject():
    spec = w.WireSpec()
    assert w._should_skip("no-reply@accounts.google.com", "Anything", spec)
    assert w._should_skip("Brew <x@morningbrew.com>", "Your receipt is ready", spec)
    assert not w._should_skip("Morning Brew <x@morningbrew.com>", "AI eats the world", spec)


def test_curly_quote_normalised_in_subject_skip():
    spec = w.WireSpec()
    # "we're updating" with a curly apostrophe must still match the skip list.
    assert w._should_skip("x@y.com", "We’re updating our terms", spec)


def test_promo_tagging():
    spec = w.WireSpec()
    assert w._is_promo("Nespresso <news@nespresso.com>", spec)
    assert not w._is_promo("Stratechery <ben@stratechery.com>", spec)


def test_config_override_lists(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"agent": {"wire": {"enabled": True, "skip_from": ["evil.com"],
                                            "hour": 8, "weekdays_only": False}}},
    )
    spec = w.load_wire_spec()
    assert spec.enabled and spec.hour == 8 and spec.weekdays_only is False
    assert spec.skip_from == ("evil.com",)
    assert w._should_skip("x@evil.com", "hi", spec)
    # a sender no longer in the (overridden) list is not skipped
    assert not w._should_skip("noreply@accounts.google.com", "hi", spec)


# --- schedule ---------------------------------------------------------------


def test_is_due_weekdays_only():
    spec = w.WireSpec(weekdays_only=True, hour=19, minute=0)
    fri = datetime.datetime(2026, 6, 19, 19, 5)   # Friday
    sat = datetime.datetime(2026, 6, 20, 19, 5)   # Saturday
    assert w.is_due(spec, fri)
    assert not w.is_due(spec, sat)                 # weekend gated off
    assert not w.is_due(spec, fri.replace(hour=12))  # before window
    assert not w.is_due(spec, fri.replace(hour=23))   # past catch-up window


def test_is_due_daily_allows_weekend():
    spec = w.WireSpec(weekdays_only=False, hour=19)
    assert w.is_due(spec, datetime.datetime(2026, 6, 20, 19, 5))  # Saturday OK


# --- HTML build + validation ------------------------------------------------


def test_validate_rejects_placeholders_and_markdown():
    ok, _ = w._validate_sections('<div class="card"><h2>X</h2><ul><li>Real thing</li></ul></div>')
    assert ok
    assert not w._validate_sections('<h2>X</h2><li>Concrete headline</li>')[0]
    assert not w._validate_sections('<h2>X</h2><li>**bold**</li>')[0]
    assert not w._validate_sections("<h2>X</h2><li>```code```</li>")[0]
    assert not w._validate_sections("no html here")[0]


def test_build_uses_template_shell_and_edition():
    items = [{"id": "1", "from": "Brew", "subject": "AI", "body": "GPT-5 shipped", "promo": False}]

    def stub(p):
        return '<div class="card"><h2>Top Stories</h2><ol><li>GPT-5 shipped at $200/mo</li></ol></div>'

    html, stories = w.build_wire_html(items, 68, complete_fn=stub,
                                      now=datetime.datetime(2026, 6, 19, 19, 0))
    assert "<!DOCTYPE html>" in html and "The Wire — #68" in html
    assert stories == 1


def test_build_falls_back_when_model_output_invalid():
    items = [{"id": "1", "from": "a@b.com", "subject": "Hi & <b>", "body": "x", "promo": False}]

    def bad(p):
        return "Concrete headline placeholder"   # fails validation

    html, _ = w.build_wire_html(items, 70, complete_fn=bad,
                                now=datetime.datetime(2026, 6, 19, 19, 0))
    # fallback renders the real subject, HTML-escaped, and is itself valid
    assert "&amp;" in html and "&lt;b&gt;" in html
    assert "Concrete headline" not in html


def test_build_fallback_when_model_unavailable(monkeypatch):
    monkeypatch.setattr("app.core.completion.select_completion", lambda *a, **k: None)
    items = [{"id": "1", "from": "a@b.com", "subject": "Story one", "body": "x", "promo": False}]
    html, stories = w.build_wire_html(items, 71, now=datetime.datetime(2026, 6, 19, 19, 0))
    assert "Story one" in html and stories == 1


def test_merge_section_cards_dedupes_and_orders():
    batch_a = ('<div class="card"><h2>🤖 AI & Tech</h2><ul>'
               "<li>GPT-5 shipped</li><li>dup item</li></ul></div>"
               '<div class="card"><h2>🛍️ Promotions</h2><ul><li>Nespresso sale</li></ul></div>')
    batch_b = ('<div class="card"><h2>💸 Fundraising & Deals</h2><ul><li>Acme raised $5M</li></ul></div>'
               '<div class="card"><h2>🤖 AI & Tech</h2><ul><li>dup item</li><li>Claude update</li></ul></div>')
    merged = w._merge_section_cards([batch_a, batch_b])
    # AI & Tech comes before Fundraising (canonical order), Promotions is last,
    # and the duplicated <li> appears once.
    assert merged.index("AI &amp; Tech" if "AI &amp;" in merged else "AI & Tech") < merged.index("Fundraising")
    assert merged.index("Fundraising") < merged.index("Promotions")
    assert merged.count("dup item") == 1


def test_chunked_path_used_for_high_volume(monkeypatch):
    # >_CHUNK_SIZE items must take the chunked path: each batch summarized, merged.
    calls = []

    def fake(prompt):
        calls.append(prompt)
        if "pick the 3 most significant" in prompt:   # Top Stories synthesis pass
            return '<div class="card"><h2>Top Stories</h2><ol><li>Big one</li></ol></div>'
        return ('<div class="card"><h2>🤖 AI & Tech</h2><ul>'
                '<li><strong>Story from batch</strong> covers GPT progress.</li></ul></div>')

    items = [{"id": str(i), "from": "Brew", "subject": f"S{i}", "body": "GPT", "promo": False}
             for i in range(w._CHUNK_SIZE + 5)]
    html, stories = w.build_wire_html(items, 80, complete_fn=fake,
                                      now=datetime.datetime(2026, 6, 19, 19, 0))
    assert len(calls) >= 2 + 1                 # ≥2 batches + 1 Top Stories pass
    assert "Top Stories" in html and "Story from batch" in html
    assert "Newsletters" not in html          # not the flat fallback


# --- edition tracking -------------------------------------------------------


def test_edition_seeds_then_bumps(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "get_var_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr("app.core.settings.get_var_dir", lambda: tmp_path)
    monkeypatch.setattr(w, "_seed_last_edition", lambda: 67)
    assert w.next_edition() == 68
    w._bump_edition(68, date="2026-06-19", emails=40, stories=55)
    assert w.read_edition_state()["lastEdition"] == 68
    assert w.next_edition() == 69


# --- orchestration: gating + empty-day --------------------------------------


def _cfg(monkeypatch, *, wire_enabled, send_enabled=True, kill=False):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {
            "agent": {
                "wire": {"enabled": wire_enabled, "weekdays_only": False},
                "send": {"enabled": send_enabled},
                "outbound_kill_switch": kill,
            }
        },
    )
    monkeypatch.setattr(w, "_accounts", lambda: ["me@x.com"])


def test_run_disabled_short_circuits(monkeypatch):
    _cfg(monkeypatch, wire_enabled=False)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    monkeypatch.setattr("app.agent.digest_tasks._period_done", lambda *a, **k: False)
    res = w.run_wire("sqlite:///x", now=datetime.datetime(2026, 6, 19, 19, 0))
    assert res["status"] == "disabled"


def test_run_empty_day_no_send_no_bump(monkeypatch):
    _cfg(monkeypatch, wire_enabled=True)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    monkeypatch.setattr("app.agent.digest_tasks._period_done", lambda *a, **k: False)
    monkeypatch.setattr("app.agent.digest_tasks.reap_stale_digest_runs", lambda *a, **k: 0)
    monkeypatch.setattr(w, "collect_wire", lambda spec, accts: ([], []))
    bumped = []
    monkeypatch.setattr(w, "_bump_edition", lambda *a, **k: bumped.append(1))
    res = w.run_wire("sqlite:///x", now=datetime.datetime(2026, 6, 19, 19, 0))
    assert res["status"] == "empty" and not bumped


def test_run_blocks_when_send_frontier_closed(monkeypatch):
    _cfg(monkeypatch, wire_enabled=True, send_enabled=False)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    monkeypatch.setattr("app.agent.digest_tasks._period_done", lambda *a, **k: False)
    monkeypatch.setattr("app.agent.digest_tasks.reap_stale_digest_runs", lambda *a, **k: 0)
    monkeypatch.setattr("app.agent.digest_tasks._undigested", lambda db, n, a, items: items)
    items = [{"id": "1", "account": "me@x.com", "from": "Brew", "subject": "AI", "body": "x", "promo": False}]
    manifest = [{"id": "1", "account": "me@x.com", "from": "Brew", "subject": "AI"}]
    monkeypatch.setattr(w, "collect_wire", lambda spec, accts: (items, manifest))
    sent = []
    monkeypatch.setattr("app.ingestion.gmail_write.send_email", lambda **k: sent.append(1))
    res = w.run_wire("sqlite:///x", now=datetime.datetime(2026, 6, 19, 19, 0))
    assert res["status"] == "blocked" and not sent


def test_force_bypasses_dedup_and_daily_claim(monkeypatch):
    _cfg(monkeypatch, wire_enabled=True)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    # _period_done would say True (already ran today) — force must ignore it.
    monkeypatch.setattr("app.agent.digest_tasks._period_done", lambda *a, **k: True)
    monkeypatch.setattr("app.agent.digest_tasks.reap_stale_digest_runs", lambda *a, **k: 0)
    # _undigested would drop everything (all already digested) — force must skip it.
    monkeypatch.setattr("app.agent.digest_tasks._undigested", lambda *a, **k: [])
    monkeypatch.setattr("app.agent.digest_tasks._claim_period", lambda *a, **k: 1)
    monkeypatch.setattr("app.agent.digest_tasks._record_digested", lambda *a, **k: None)
    monkeypatch.setattr("app.agent.digest_tasks._update_run", lambda *a, **k: None)
    items = [{"id": "1", "account": "me@x.com", "from": "Brew", "subject": "AI", "body": "x", "promo": False}]
    monkeypatch.setattr(w, "collect_wire", lambda spec, accts: (items, list(items)))
    monkeypatch.setattr(w, "build_wire_html", lambda *a, **k: ("<html></html>", 1))
    monkeypatch.setattr(w, "_archive", lambda *a, **k: 1)
    monkeypatch.setattr(w, "_bump_edition", lambda *a, **k: None)

    class _R:
        message_id = "m1"
    monkeypatch.setattr("app.ingestion.gmail_write.send_email", lambda **k: _R())
    res = w.run_wire("sqlite:///x", now=datetime.datetime(2026, 6, 19, 19, 0), force=True)
    assert res["status"] == "sent" and res["count"] == 1   # included despite dedup/claim


def test_run_kill_switch_blocks(monkeypatch):
    _cfg(monkeypatch, wire_enabled=True, send_enabled=True, kill=True)
    monkeypatch.setattr("app.db.bootstrap.ensure_agent_schema", lambda *a, **k: True)
    monkeypatch.setattr("app.agent.digest_tasks._period_done", lambda *a, **k: False)
    monkeypatch.setattr("app.agent.digest_tasks.reap_stale_digest_runs", lambda *a, **k: 0)
    monkeypatch.setattr("app.agent.digest_tasks._undigested", lambda db, n, a, items: items)
    items = [{"id": "1", "account": "me@x.com", "from": "Brew", "subject": "AI", "body": "x", "promo": False}]
    monkeypatch.setattr(w, "collect_wire", lambda spec, accts: (items, items))
    res = w.run_wire("sqlite:///x", now=datetime.datetime(2026, 6, 19, 19, 0))
    assert res["status"] == "blocked" and "kill" in res["detail"]


# --- pagination -------------------------------------------------------------


def test_list_account_paginates_past_one_page(monkeypatch):
    # Gmail caps a page at 500; _list_account must follow nextPageToken until the
    # window is exhausted (or the cap is hit) instead of truncating at one page.
    import json as _json

    pages = {
        None: {"messages": [{"id": f"a{i}", "from": "Brew", "subject": "x"} for i in range(500)],
               "nextPageToken": "P2"},
        "P2": {"messages": [{"id": f"b{i}", "from": "Brew", "subject": "x"} for i in range(120)]},
    }

    def fake_run(cmd, timeout):
        tok = cmd[cmd.index("--page") + 1] if "--page" in cmd else None
        return _json.dumps(pages[tok])

    monkeypatch.setattr(w, "_run", fake_run)
    spec = w.WireSpec(max_emails=1000)
    out = w._list_account("me@x.com", spec)
    assert len(out) == 620                      # both pages, not capped at 500


def test_list_account_respects_max_cap(monkeypatch):
    import json as _json

    def fake_run(cmd, timeout):
        return _json.dumps({"messages": [{"id": f"a{i}", "from": "B", "subject": "x"}
                                         for i in range(500)], "nextPageToken": "more"})

    monkeypatch.setattr(w, "_run", fake_run)
    out = w._list_account("me@x.com", w.WireSpec(max_emails=300))
    assert len(out) == 300                      # stops at the cap despite more pages


# --- archive exclusions -----------------------------------------------------


def test_archive_excludes_allowlisted_sender(monkeypatch):
    # archive_exclusions is config-driven (empty by default); set it explicitly.
    spec = w.WireSpec(archive_exclusions=("keep-me.example", "vip newsletter"))
    calls = []
    monkeypatch.setattr("app.ingestion.gmail_write.modify_message_labels",
                        lambda **k: calls.append(k["message_id"]))
    manifest = [
        {"id": "keep", "account": "me@x.com", "from": "VIP Newsletter <hi@keep-me.example>", "subject": "x"},
        {"id": "arch", "account": "me@x.com", "from": "Morning Brew <x@brew.com>", "subject": "y"},
    ]
    archived = w._archive(manifest, spec)
    assert archived == 1 and calls == ["arch"]   # allow-listed sender never archived
