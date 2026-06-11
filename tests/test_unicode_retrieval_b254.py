"""b254 (pass-9 finding P9-1): retrieval tokenization is unicode-aware.

The old query tokenizer kept only ASCII [a-z0-9] runs while the FTS5 tables
index with FTS5's unicode tokenizer: Arabic inbound text tokenized to
NOTHING (zero exemplars — the drafter ran blind, and semantic reranking
can't backfill because it only re-ranks the FTS candidate pool), and umlaut
words shattered into noise tokens ("Frühstück" → fr/hst/ck).
"""

from __future__ import annotations

import sqlite3

from app.retrieval.service import _fts5_query, _tokenize


def test_arabic_tokenizes_instead_of_vanishing():
    tokens = _tokenize("مرحبا، هل يمكنك إرسال العرض؟")
    assert len(tokens) >= 4  # previously: []
    assert "إرسال" in tokens
    assert _fts5_query(tokens)  # previously: "" → early-return, no retrieval


def test_german_umlauts_survive_whole():
    tokens = _tokenize("Übermorgen Frühstück? Können wir reden")
    assert "frühstück" in tokens  # previously shattered: fr/hst/ck
    assert "übermorgen" in tokens
    assert "können" in tokens


def test_punctuation_and_operators_still_stripped():
    tokens = _tokenize('hello "OR 1=1 -- (NEAR) world')
    assert '"' not in "".join(tokens)
    assert "--" not in tokens
    # lowercased barewords can't act as FTS5 operators (which are uppercase)
    assert all(t == t.casefold() for t in tokens)
    q = _fts5_query(tokens)
    assert '"' not in q and "(" not in q


def test_end_to_end_fts5_match_for_non_ascii():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t VALUES (?)", ("مرحبا، هل يمكنك إرسال العرض النهائي؟",))
    conn.execute("INSERT INTO t VALUES (?)", ("Können wir übermorgen zum Frühstück reden?",))

    ar = _fts5_query(_tokenize("هل يمكنك إرسال العرض"))
    de = _fts5_query(_tokenize("Frühstück übermorgen?"))
    assert conn.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (ar,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (de,)).fetchone()[0] == 1


def test_english_tokenization_unchanged():
    assert _tokenize("Re: the Q2 invoice follow-up") == ["re", "the", "q2", "invoice", "follow", "up"]
