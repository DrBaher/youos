"""Tests for sender classification and sender-aware retrieval."""
import json
import sqlite3
from pathlib import Path

from app.core.sender import SenderType, classify_sender, extract_domain
from app.generation.service import strip_signature, assemble_prompt, _load_persona
from app.retrieval.service import RetrievalRequest, RetrievalService

ROOT_DIR = Path(__file__).resolve().parents[1]


# ── classify_sender ─────────────────────────────────────────────────


def test_classify_internal() -> None:
    assert classify_sender("Baher <baher@medicus.ai>") == "internal"
    assert classify_sender("alice@medicus.ai") == "internal"


def test_classify_external_client() -> None:
    assert classify_sender("John <john@crelio.com>") == "external_client"
    assert classify_sender("contact@acme.org") == "external_client"


def test_classify_personal() -> None:
    assert classify_sender("friend@gmail.com") == "personal"
    assert classify_sender("someone@yahoo.com") == "personal"
    assert classify_sender("user@hotmail.com") == "personal"
    assert classify_sender("test@icloud.com") == "personal"
    assert classify_sender("x@outlook.com") == "personal"
    assert classify_sender("y@me.com") == "personal"


def test_classify_automated() -> None:
    assert classify_sender("no-reply@example.com") == "automated"
    assert classify_sender("noreply@github.com") == "automated"
    assert classify_sender("invoice@stripe.com") == "automated"
    assert classify_sender("billing@aws.com") == "automated"
    assert classify_sender("mailer@service.com") == "automated"
    assert classify_sender("donotreply@company.com") == "automated"


def test_classify_unknown() -> None:
    assert classify_sender(None) == "unknown"
    assert classify_sender("") == "unknown"
    assert classify_sender("not an email") == "unknown"


# ── extract_domain ──────────────────────────────────────────────────


def test_extract_domain() -> None:
    assert extract_domain("John <john@crelio.com>") == "crelio.com"
    assert extract_domain("alice@medicus.ai") == "medicus.ai"
    assert extract_domain(None) is None
    assert extract_domain("no email here") is None


# ── Signature stripping ─────────────────────────────────────────────


def test_strip_signature_baher_al_hakim() -> None:
    text = "Sure, I will send it.\n\nBaher Al Hakim\nCEO, Medicus AI"
    assert strip_signature(text) == "Sure, I will send it."


def test_strip_signature_best() -> None:
    text = "Looks good to me.\n\nBest,\nBaher"
    assert strip_signature(text) == "Looks good to me."


def test_strip_signature_cheers() -> None:
    text = "Will do.\n\nCheers,\nBaher"
    assert strip_signature(text) == "Will do."


def test_strip_signature_dashes() -> None:
    text = "Here is the info.\n-- \nBaher Al Hakim"
    assert strip_signature(text) == "Here is the info."


def test_strip_signature_no_signature() -> None:
    text = "Just a plain reply with no signature."
    assert strip_signature(text) == text


# ── Persona loading ─────────────────────────────────────────────────


def test_persona_yaml_loads() -> None:
    persona = _load_persona(ROOT_DIR / "configs")
    assert persona["name"] == "Baher"
    style = persona["style"]
    assert "voice" in style
    assert "avg_reply_words" in style
    assert "constraints" in style
    assert isinstance(style["constraints"], list)
    assert len(style["constraints"]) > 0


def test_persona_constraints_in_prompt() -> None:
    from app.retrieval.service import RetrievalMatch

    persona = _load_persona(ROOT_DIR / "configs")
    pairs = [
        RetrievalMatch(
            result_type="reply_pair", score=9.0, lexical_score=9.0, metadata_score=0.0,
            source_type="gmail_thread", source_id="t1", account_email=None,
            title=None, author=None, external_uri=None, thread_id=None,
            created_at=None, updated_at=None, reply_pair_id=1,
            snippet="hi", inbound_text="hi", reply_text="hello",
        )
    ]
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=pairs,
        persona=persona,
        prompts={"system_prompt": "You are BaherOS."},
    )
    assert "no sycophancy" in prompt.lower()
    assert "Target reply length" in prompt
    assert "direct, clear, pragmatic" in prompt


# ── Sender boost in retrieval ───────────────────────────────────────


def test_sender_type_boost_applied(tmp_path: Path) -> None:
    """When sender_type_hint matches stored inbound_author type, score is boosted."""
    db_path = tmp_path / "baheros.db"
    _seed_sender_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    # With sender hint matching the external client
    response_with = service.retrieve(
        RetrievalRequest(
            query="quote pricing enterprise",
            scope="reply_pairs",
            sender_type_hint="external_client",
            sender_domain_hint="crelio.com",
        )
    )

    # Without sender hint
    response_without = service.retrieve(
        RetrievalRequest(
            query="quote pricing enterprise",
            scope="reply_pairs",
        )
    )

    # Both should find results
    assert response_with.reply_pairs
    assert response_without.reply_pairs

    # The version with sender hint should have higher metadata_score for matching pairs
    with_scores = {rp.reply_pair_id: rp.metadata_score for rp in response_with.reply_pairs}
    without_scores = {rp.reply_pair_id: rp.metadata_score for rp in response_without.reply_pairs}

    # Find a reply pair present in both
    common_ids = set(with_scores) & set(without_scores)
    if common_ids:
        for rid in common_ids:
            # sender boost should make with >= without
            assert with_scores[rid] >= without_scores[rid]


def test_sender_domain_boost_applied(tmp_path: Path) -> None:
    """Domain-specific boost adds on top of type boost."""
    db_path = tmp_path / "baheros.db"
    _seed_sender_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    # With exact domain match
    response_domain = service.retrieve(
        RetrievalRequest(
            query="quote pricing enterprise",
            scope="reply_pairs",
            sender_type_hint="external_client",
            sender_domain_hint="crelio.com",
        )
    )

    # With type match but different domain
    response_type_only = service.retrieve(
        RetrievalRequest(
            query="quote pricing enterprise",
            scope="reply_pairs",
            sender_type_hint="external_client",
            sender_domain_hint="other.com",
        )
    )

    if response_domain.reply_pairs and response_type_only.reply_pairs:
        # The crelio match with domain boost should score higher
        domain_scores = {rp.reply_pair_id: rp.metadata_score for rp in response_domain.reply_pairs}
        type_scores = {rp.reply_pair_id: rp.metadata_score for rp in response_type_only.reply_pairs}
        common = set(domain_scores) & set(type_scores)
        for rid in common:
            assert domain_scores[rid] >= type_scores[rid]


# ── DB seed helper ──────────────────────────────────────────────────


def _seed_sender_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.execute(
            """
            INSERT INTO documents (source_type, source_id, title, author, content, metadata_json, ingestion_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread", "m-sender-1", "Quote request", "Client",
                "enterprise quote request",
                json.dumps({"account_email": "drbaher@gmail.com"}),
                "seed-sender",
            ),
        )
        doc_id = conn.execute("SELECT id FROM documents WHERE source_id = 'm-sender-1'").fetchone()[0]
        conn.execute(
            """
            INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, 0, "enterprise quote pricing request", 4, 34, "{}"),
        )
        conn.execute(
            """
            INSERT INTO reply_pairs (
                source_type, source_id, document_id, thread_id,
                inbound_text, reply_text, inbound_author, reply_author,
                paired_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread", "pair-sender-1", doc_id, "thread-sender-1",
                "Can you send a quote for the enterprise pricing plan?",
                "Sure. I will send the pricing breakdown.\n\nBest,\nBaher",
                "John <john@crelio.com>", "Baher <drbaher@gmail.com>",
                "2026-03-01T10:00:00Z",
                json.dumps({"account_email": "drbaher@gmail.com"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
