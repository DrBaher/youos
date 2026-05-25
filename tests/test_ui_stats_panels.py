"""Stats page surfaces previously-unused /stats/data keys (UI PR C).

draft_events, persona_adapters, and embedding_coverage_by_table were returned
by /stats/data but never rendered. These pin that the stats page now has the
panels + the JS that reads those keys. (outcome_deltas was already wired.)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _stats_html() -> str:
    return TestClient(app).get("/stats").text


def test_draft_events_panel_present():
    html = _stats_html()
    assert 'id="draftEventsCard"' in html
    assert "d.draft_events" in html
    assert 'id="deBreakdown"' in html


def test_persona_adapters_panel_present():
    html = _stats_html()
    assert 'id="personaAdaptersCard"' in html
    assert "d.persona_adapters" in html


def test_stats_data_exposes_the_keys():
    # Guard the backend contract these panels depend on.
    body = TestClient(app).get("/stats/data").json()
    assert "draft_events" in body
    assert "persona_adapters" in body
