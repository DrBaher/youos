"""Draft UI surfaces the new DraftResponse fields (UI PR B).

length_flag / repairs / candidates were computed but never shown. These pin
that the draft page has the rendering targets + logic and that the stream
done-event carries the fields (full SSE behavior is verified on a running
instance). Guards against the wiring silently regressing.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _feedback_html() -> str:
    return TestClient(app).get("/feedback").text


def test_draft_meta_containers_present():
    html = _feedback_html()
    assert 'id="draftBadges"' in html
    assert 'id="draftCandidates"' in html


def test_render_draft_meta_handles_new_fields():
    html = _feedback_html()
    assert "function renderDraftMeta" in html
    # the renderer reads each of the previously-dropped fields
    assert "data.length_flag" in html
    assert "data.repairs" in html
    assert "data.candidates" in html
    assert "yos-candidate" in html  # multi-candidate picker markup


def test_both_paths_invoke_renderer():
    html = _feedback_html()
    # streaming done-event captures the fields, and both paths render them
    assert "payload.length_flag" in html
    assert "renderDraftMeta(streamMeta)" in html
    assert "renderDraftMeta(data)" in html


def test_stream_done_event_includes_draft_quality_fields():
    # The stream endpoint's done payload must carry the fields so the streaming
    # path (the primary one) can render them.
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "stream_routes.py").read_text(encoding="utf-8")
    assert '"length_flag": length_flag' in src
    assert '"repairs": repairs' in src
    assert '"candidates": candidates' in src
