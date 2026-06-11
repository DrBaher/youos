"""b248: one corrupt .json must not sink a docs directory import (gmail got
this tolerance in b129; docs never did — a torn cache file poisoned every
subsequent dir import until manually deleted)."""

from __future__ import annotations

import json

from app.ingestion.google_docs import _load_doc_payloads


def test_dir_import_skips_corrupt_file_keeps_rest(tmp_path):
    good = {"snapshot_type": "gog_google_doc", "documentId": "d1", "title": "T"}
    (tmp_path / "good.json").write_text(json.dumps(good))
    (tmp_path / "torn.json").write_text('{"document": {"docu')  # torn mid-write

    result = _load_doc_payloads(tmp_path, live=None)
    assert len(result.payloads) == 1  # the good file survived the bad neighbor
