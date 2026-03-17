"""Tests for sender profiles feature."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.build_sender_profiles import (
    build_profiles,
    company_from_domain,
    extract_display_name,
    extract_email,
    extract_topics,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create a test database with schema."""
    db = tmp_path / "test.db"
    schema = (Path(__file__).resolve().parents[1] / "docs" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db


def _seed_reply_pairs(db_path: Path, pairs: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    for p in pairs:
        conn.execute(
            "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, "
            "inbound_author, reply_author, paired_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                p.get("source_type", "gmail"),
                p["source_id"],
                p["inbound_text"],
                p["reply_text"],
                p["inbound_author"],
                p.get("reply_author", "Baher Al Hakim <baher@medicus.ai>"),
                p.get("paired_at", "2025-01-15T10:00:00Z"),
            ),
        )
    conn.commit()
    conn.close()


# ── Author parsing tests ──────────────────────────────────────────────


class TestAuthorParsing:
    def test_extract_email_from_angle_bracket(self):
        assert extract_email("John Smith <john@crelio.com>") == "john@crelio.com"

    def test_extract_email_plain(self):
        assert extract_email("john@crelio.com") == "john@crelio.com"

    def test_extract_email_none(self):
        assert extract_email("no email here") is None

    def test_extract_email_empty(self):
        assert extract_email("") is None

    def test_extract_display_name(self):
        assert extract_display_name("John Smith <john@crelio.com>") == "John Smith"

    def test_extract_display_name_quoted(self):
        assert extract_display_name('"John Smith" <john@crelio.com>') == "John Smith"

    def test_extract_display_name_plain_email(self):
        assert extract_display_name("john@crelio.com") is None

    def test_extract_display_name_empty_name(self):
        assert extract_display_name("<john@crelio.com>") is None


# ── Company inference tests ───────────────────────────────────────────


class TestCompanyInference:
    def test_company_from_business_domain(self):
        assert company_from_domain("crelio.com") == "Crelio"

    def test_company_from_multi_word_domain(self):
        assert company_from_domain("medicus.ai") == "Medicus"

    def test_company_from_hyphenated_domain(self):
        assert company_from_domain("my-company.com") == "My Company"

    def test_company_from_personal_domain_returns_none(self):
        assert company_from_domain("gmail.com") is None

    def test_company_from_none_returns_none(self):
        assert company_from_domain(None) is None


# ── Topic extraction tests ───────────────────────────────────────────


class TestTopicExtraction:
    def test_extracts_top_keywords(self):
        subjects = [
            "Re: Integration API docs",
            "Re: Integration timeline",
            "Fw: API access request",
        ]
        topics = extract_topics(subjects, top_n=3)
        assert "integration" in topics
        assert "api" in topics

    def test_strips_re_fw_prefix(self):
        subjects = ["Re: Fwd: Meeting notes"]
        topics = extract_topics(subjects, top_n=3)
        # "re" and "fwd" should not appear as topics
        assert "re" not in topics
        assert "fwd" not in topics

    def test_empty_subjects(self):
        assert extract_topics([]) == []


# ── build_profiles tests ─────────────────────────────────────────────


class TestBuildProfiles:
    def test_builds_profiles_from_corpus(self, db_path: Path):
        _seed_reply_pairs(db_path, [
            {
                "source_id": "pair1",
                "inbound_text": "Can we get a quote?",
                "reply_text": "Sure, let me prepare that for you.",
                "inbound_author": "John Smith <john@crelio.com>",
                "paired_at": "2025-01-10T10:00:00Z",
            },
            {
                "source_id": "pair2",
                "inbound_text": "Follow up on quote",
                "reply_text": "Here is the quote attached.",
                "inbound_author": "John Smith <john@crelio.com>",
                "paired_at": "2025-02-15T10:00:00Z",
            },
        ])
        new, updated = build_profiles(db_path)
        assert new == 1
        assert updated == 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sender_profiles WHERE email = 'john@crelio.com'").fetchone()
        conn.close()

        assert row is not None
        assert row["display_name"] == "John Smith"
        assert row["domain"] == "crelio.com"
        assert row["company"] == "Crelio"
        assert row["sender_type"] == "external_client"
        assert row["reply_count"] == 2
        assert row["avg_reply_words"] is not None
        assert row["first_seen"] == "2025-01-10T10:00:00Z"
        assert row["last_seen"] == "2025-02-15T10:00:00Z"

    def test_updates_existing_profile(self, db_path: Path):
        _seed_reply_pairs(db_path, [
            {
                "source_id": "pair1",
                "inbound_text": "Hello",
                "reply_text": "Hi there",
                "inbound_author": "Jane <jane@example.com>",
            },
        ])
        new1, upd1 = build_profiles(db_path)
        assert new1 == 1

        # Add another pair and rebuild
        _seed_reply_pairs(db_path, [
            {
                "source_id": "pair2",
                "inbound_text": "Another message",
                "reply_text": "Got it thanks",
                "inbound_author": "Jane <jane@example.com>",
            },
        ])
        new2, upd2 = build_profiles(db_path)
        assert new2 == 0
        assert upd2 == 1

    def test_dry_run_does_not_write(self, db_path: Path):
        _seed_reply_pairs(db_path, [
            {
                "source_id": "pair1",
                "inbound_text": "Test",
                "reply_text": "Reply",
                "inbound_author": "Test User <test@test.com>",
            },
        ])
        new, _ = build_profiles(db_path, dry_run=True)
        assert new == 1

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM sender_profiles").fetchone()[0]
        conn.close()
        assert count == 0

    def test_limit_restricts_processing(self, db_path: Path):
        for i in range(5):
            _seed_reply_pairs(db_path, [
                {
                    "source_id": f"pair{i}",
                    "inbound_text": f"Message {i}",
                    "reply_text": f"Reply {i}",
                    "inbound_author": f"User{i} <user{i}@domain{i}.com>",
                },
            ])
        new, _ = build_profiles(db_path, limit=2)
        assert new == 2


# ── Relationship note update test ────────────────────────────────────


class TestRelationshipNote:
    def test_note_update_persists(self, db_path: Path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sender_profiles (email, sender_type) VALUES (?, ?)",
            ("john@crelio.com", "external_client"),
        )
        conn.commit()

        conn.execute(
            "UPDATE sender_profiles SET relationship_note = ? WHERE email = ?",
            ("integration partner, warm relationship", "john@crelio.com"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT relationship_note FROM sender_profiles WHERE email = ?",
            ("john@crelio.com",),
        ).fetchone()
        conn.close()
        assert row[0] == "integration partner, warm relationship"


# ── Generation prompt includes sender context ────────────────────────


class TestGenerationWithSenderContext:
    def test_prompt_includes_sender_context_when_profile_found(self):
        from app.generation.service import _format_sender_context, assemble_prompt

        profile = {
            "email": "john@crelio.com",
            "display_name": "John Smith",
            "company": "Crelio",
            "sender_type": "external_client",
            "relationship_note": "integration partner",
            "reply_count": 14,
            "avg_reply_words": 38.2,
            "topics": ["integration", "api"],
        }
        sender_context = _format_sender_context(profile)

        prompt = assemble_prompt(
            inbound_message="Can we get a quote?",
            reply_pairs=[],
            persona={"style": {"voice": "direct"}},
            prompts={"system_prompt": "You are BaherOS."},
            sender_context=sender_context,
        )

        assert "[SENDER CONTEXT]" in prompt
        assert "John Smith" in prompt
        assert "Crelio" in prompt
        assert "external_client" in prompt
        assert "integration partner" in prompt
        assert "14" in prompt

    def test_prompt_no_sender_context_when_none(self):
        from app.generation.service import assemble_prompt

        prompt = assemble_prompt(
            inbound_message="Hello",
            reply_pairs=[],
            persona={"style": {"voice": "direct"}},
            prompts={"system_prompt": "You are BaherOS."},
        )
        assert "[SENDER CONTEXT]" not in prompt


# ── API route tests ──────────────────────────────────────────────────


@pytest.fixture()
def test_app(db_path: Path):
    """Create a test FastAPI app with the sender routes."""
    from app.core.settings import Settings
    from app.main import create_app

    app = create_app()
    app.state.settings = Settings(
        database_url=f"sqlite:///{db_path}",
        configs_dir=Path(__file__).resolve().parents[1] / "configs",
    )
    return app


@pytest.fixture()
def client(test_app) -> TestClient:
    return TestClient(test_app)


class TestSenderRoutes:
    def test_lookup_unknown_sender(self, client: TestClient):
        resp = client.get("/senders/lookup", params={"email": "nobody@nowhere.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is False

    def test_lookup_existing_profile(self, client: TestClient, db_path: Path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sender_profiles (email, display_name, domain, company, sender_type, reply_count, avg_reply_words, topics_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("john@crelio.com", "John Smith", "crelio.com", "Crelio", "external_client", 14, 38.2, '["integration","api"]'),
        )
        conn.commit()
        conn.close()

        resp = client.get("/senders/lookup", params={"email": "john@crelio.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["profile"]["reply_count"] == 14
        assert data["profile"]["company"] == "Crelio"

    def test_search_by_company(self, client: TestClient, db_path: Path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sender_profiles (email, company, sender_type) VALUES (?, ?, ?)",
            ("john@crelio.com", "Crelio", "external_client"),
        )
        conn.commit()
        conn.close()

        resp = client.get("/senders/search", params={"q": "crelio"})
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_update_note(self, client: TestClient, db_path: Path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sender_profiles (email, sender_type) VALUES (?, ?)",
            ("john@crelio.com", "external_client"),
        )
        conn.commit()
        conn.close()

        resp = client.post(
            "/senders/john@crelio.com/note",
            json={"relationship_note": "warm relationship"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify persisted
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT relationship_note FROM sender_profiles WHERE email = ?",
            ("john@crelio.com",),
        ).fetchone()
        conn.close()
        assert row[0] == "warm relationship"

    def test_update_note_creates_profile_if_missing(self, client: TestClient):
        resp = client.post(
            "/senders/newperson@example.com/note",
            json={"relationship_note": "new contact"},
        )
        assert resp.status_code == 200

    def test_sender_history(self, client: TestClient, db_path: Path):
        _seed_reply_pairs(db_path, [
            {
                "source_id": "h1",
                "inbound_text": "Hello from history",
                "reply_text": "Hi back",
                "inbound_author": "jane@example.com",
            },
        ])
        resp = client.get("/senders/jane@example.com/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) == 1
        assert "Hello from history" in data["history"][0]["inbound_snippet"]

    def test_lookup_infers_from_corpus(self, client: TestClient, db_path: Path):
        _seed_reply_pairs(db_path, [
            {
                "source_id": "inf1",
                "inbound_text": "Hello",
                "reply_text": "Hi there friend",
                "inbound_author": "Jane Doe <jane@inferred.com>",
            },
        ])
        resp = client.get("/senders/lookup", params={"email": "jane@inferred.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data.get("inferred") is True
        assert data["profile"]["reply_count"] == 1
