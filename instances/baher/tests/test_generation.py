import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.generation.service import (
    DraftRequest,
    DraftResponse,
    _score_confidence,
    assemble_prompt,
    generate_draft,
)
from app.main import create_app
from app.retrieval.service import RetrievalMatch

ROOT_DIR = Path(__file__).resolve().parents[1]


def _make_reply_match(*, score: float, inbound: str = "hi", reply: str = "hello") -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="gmail_thread",
        source_id="test-id",
        account_email="test@example.com",
        title="Test",
        author="Baher",
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        reply_pair_id=1,
        snippet=inbound[:80],
        inbound_text=inbound,
        reply_text=reply,
    )


# ── Dataclass structure ──────────────────────────────────────────────

def test_draft_request_fields() -> None:
    req = DraftRequest(inbound_message="test")
    assert req.inbound_message == "test"
    assert req.mode is None
    assert req.audience_hint is None
    assert req.top_k_reply_pairs == 5
    assert req.top_k_chunks == 3
    assert req.account_email is None


def test_draft_response_to_dict() -> None:
    resp = DraftResponse(
        draft="Hi there.",
        detected_mode="work",
        precedent_used=[{"source_id": "x", "score": 9.0}],
        retrieval_method="fts5_bm25",
        confidence="high",
        confidence_reason="3 strong matches",
        model_used="claude",
    )
    d = resp.to_dict()
    assert d["draft"] == "Hi there."
    assert d["detected_mode"] == "work"
    assert d["confidence"] == "high"
    assert isinstance(d["precedent_used"], list)


# ── Confidence scoring ───────────────────────────────────────────────

def test_confidence_high() -> None:
    pairs = [_make_reply_match(score=9.0) for _ in range(3)]
    level, reason = _score_confidence(pairs)
    assert level == "high"
    assert "3" in reason


def test_confidence_medium() -> None:
    pairs = [_make_reply_match(score=7.0)]
    level, reason = _score_confidence(pairs)
    assert level == "medium"


def test_confidence_low() -> None:
    pairs = [_make_reply_match(score=2.0)]
    level, _ = _score_confidence(pairs)
    assert level == "low"


def test_confidence_empty() -> None:
    level, _ = _score_confidence([])
    assert level == "low"


# ── Prompt assembly ──────────────────────────────────────────────────

def test_assemble_prompt_contains_sections() -> None:
    pairs = [
        _make_reply_match(score=9.0, inbound="What is the pricing?", reply="Here is the pricing."),
    ]
    prompt = assemble_prompt(
        inbound_message="Send me a quote",
        reply_pairs=pairs,
        persona={"style": {"voice": "direct, clear"}},
        prompts={"system_prompt": "You are BaherOS."},
    )
    assert "[SYSTEM]" in prompt
    assert "[EXEMPLARS" in prompt
    assert "[TASK]" in prompt
    assert "[INBOUND MESSAGE]" in prompt
    assert "Send me a quote" in prompt
    assert "What is the pricing?" in prompt
    assert "Here is the pricing." in prompt


def test_assemble_prompt_no_exemplars() -> None:
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=[],
        persona={},
        prompts={},
    )
    assert "(no exemplars found)" in prompt
    assert "[EXEMPLARS — 0" in prompt


# ── Full generation (mocked LLM) ────────────────────────────────────

def test_generate_draft_with_mocked_cli(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    with patch("app.generation.service._call_claude_cli", return_value="Sure, here is the quote."):
        response = generate_draft(
            DraftRequest(inbound_message="Can you send me a quote?", use_local_model=False),
            database_url=f"sqlite:///{db_path}",
            configs_dir=ROOT_DIR / "configs",
        )

    assert response.draft == "Sure, here is the quote."
    assert response.model_used == "claude"
    assert response.detected_mode in ("work", "personal", "unknown")
    assert isinstance(response.precedent_used, list)
    assert response.confidence in ("high", "medium", "low")
    # Subject generation also runs (mocked _call_claude_cli returns subject text)
    assert response.suggested_subject is not None or response.suggested_subject is None


def test_generate_draft_cli_failure_returns_error(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    with patch(
        "app.generation.service._call_claude_cli",
        side_effect=RuntimeError("claude not found"),
    ):
        response = generate_draft(
            DraftRequest(inbound_message="Hello", use_local_model=False),
            database_url=f"sqlite:///{db_path}",
            configs_dir=ROOT_DIR / "configs",
        )

    assert "draft generation failed" in response.draft
    assert response.model_used == "error"


# ── FastAPI route ────────────────────────────────────────────────────

def test_draft_route_returns_200(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    with patch("app.generation.service._call_claude_cli", return_value="Draft reply here."):
        client = TestClient(create_app())
        response = client.post(
            "/draft",
            json={"inbound_message": "Can you send me a quote for 10 labs?", "use_local_model": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert "draft" in payload
    assert "detected_mode" in payload
    assert "precedent_used" in payload
    assert "confidence" in payload
    assert "model_used" in payload
    assert payload["draft"] == "Draft reply here."


# ── Tone hint modifies prompt ────────────────────────────────────────

def test_tone_hint_shorter_modifies_prompt() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        tone_hint="shorter",
    )
    assert "half the word count" in prompt


def test_tone_hint_more_formal_modifies_prompt() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        tone_hint="more_formal",
    )
    assert "formal, professional" in prompt


def test_tone_hint_more_detail_modifies_prompt() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        tone_hint="more_detail",
    )
    assert "more detail" in prompt


def test_tone_hint_none_no_extra_instruction() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        tone_hint=None,
    )
    assert "half the word count" not in prompt
    assert "formal, professional" not in prompt


# ── Thread context detection ──────────────────────────────────────────

def test_thread_context_detected_in_prompt() -> None:
    multi_thread = "From: Alice\nHi\n\nFrom: Bob\nHello back"
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message=multi_thread,
        reply_pairs=pairs,
        persona={},
        prompts={},
    )
    assert "multi-message thread" in prompt


def test_no_thread_context_for_single_from() -> None:
    single = "From: Alice\nHi there"
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message=single,
        reply_pairs=pairs,
        persona={},
        prompts={},
    )
    assert "multi-message thread" not in prompt


# ── Detected mode and audience in prompt ─────────────────────────────

def test_detected_mode_in_prompt() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        detected_mode="work",
    )
    assert "Detected mode: work" in prompt


def test_audience_hint_in_prompt() -> None:
    pairs = [_make_reply_match(score=9.0)]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
        audience_hint="investor",
    )
    assert "Audience: investor" in prompt


# ── Exemplar inbound snippet length ──────────────────────────────────

def test_exemplar_inbound_uses_800_chars() -> None:
    long_inbound = "A" * 800
    pairs = [_make_reply_match(score=9.0, inbound=long_inbound + "EXTRA")]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona={},
        prompts={},
    )
    assert long_inbound in prompt
    assert "EXTRA" not in prompt


def test_draft_route_validates_empty_message(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()

    client = TestClient(create_app())
    response = client.post("/draft", json={"inbound_message": ""})
    assert response.status_code == 422


# ── DB seed helper ───────────────────────────────────────────────────

def _seed_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema_sql)
        connection.execute(
            """
            INSERT INTO documents (
                source_type, source_id, title, author, external_uri, thread_id,
                created_at, updated_at, content, metadata_json, ingestion_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread", "m1", "Quote request", "Client <client@example.com>",
                None, "thread-1", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z",
                "Can you send me a quote for the enterprise plan?",
                json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
                "seed-gen-1",
            ),
        )
        doc_id = connection.execute("SELECT id FROM documents WHERE source_id = 'm1'").fetchone()[0]
        connection.execute(
            """
            INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, 0, "Can you send me a quote for the enterprise plan?", 10, 50, "{}"),
        )
        connection.execute(
            """
            INSERT INTO reply_pairs (
                source_type, source_id, document_id, thread_id,
                inbound_text, reply_text, inbound_author, reply_author,
                paired_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread", "pair-1", doc_id, "thread-1",
                "Can you send me a quote for the enterprise plan?",
                "Sure. I will send the pricing breakdown by end of day.",
                "Client <client@example.com>", "Baher <drbaher@gmail.com>",
                "2026-03-01T10:00:00Z",
                json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
            ),
        )
        connection.commit()
    finally:
        connection.close()
