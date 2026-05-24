"""Every ingestion module must import cleanly.

Regression guard: `gmail_threads` once had a malformed regex (`+1`) that raised
`re.PatternError` at import time, silently disabling all Gmail ingestion. A bare
import test catches that whole class of module-load failures.
"""

from __future__ import annotations

import importlib

import pytest

MODULES = [
    "app.ingestion.gmail_threads",
    "app.ingestion.google_docs",
    "app.ingestion.whatsapp",
    "app.ingestion.models",
    "app.ingestion.run_log",
]


@pytest.mark.parametrize("module", MODULES)
def test_ingestion_module_imports(module):
    importlib.import_module(module)
