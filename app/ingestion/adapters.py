"""Pluggable Google Workspace data sources for ingestion.

Gmail-thread and Google-Doc ingestion fetch their raw data through a
backend-agnostic :class:`GoogleWorkspaceSource`. Today the only implementation
is :class:`GogSource`, which shells out to the OpenClaw ``gog`` CLI exactly as
before — so this layer introduces **zero behavior change**.

The seam exists so YouOS can decouple from ``gog``. Two further backends are
planned and will live alongside :class:`GogSource` here:

- ``gws`` — Google's own open-source Workspace CLI (single-account, JSON-by-
  default, ``gws <service> <resource> <method> --params '{...}'``).
- ``native`` — a direct Google-API client (``google-api-python-client`` +
  ``google-auth-oauthlib``), no external CLI.

Each method returns the **canonical payload** the ingestion normalizers already
consume (``_normalize_thread_payload`` / ``_wrap_live_doc_payload`` etc.) —
today that shape *is* what ``gog --json`` emits. Future backends satisfy the
same contract by mapping their responses onto it; ``GogSource`` satisfies it by
construction.

The active backend is selected by ``ingestion.google_backend`` in
``youos_config.yaml`` and defaults to ``gog`` so existing instances are
unchanged.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.config import get_ingestion_google_backend

SUPPORTED_BACKENDS = ("gog", "gws", "native")


@runtime_checkable
class GoogleWorkspaceSource(Protocol):
    """Backend that fetches Gmail threads and Google Docs for ingestion."""

    name: str

    # --- Gmail ---
    def search_threads(self, *, account: str, query: str, max_threads: int | None) -> list[dict[str, Any]]:
        """Return search-result dicts (each carrying an extractable thread id)."""
        ...

    def get_thread(self, *, account: str, thread_id: str) -> dict[str, Any]:
        """Return the canonical normalized thread payload for ``thread_id``."""
        ...

    # --- Google Docs / Drive ---
    def drive_search(self, *, account: str, query: str, max_docs: int | None, raw_query: bool) -> list[dict[str, Any]]:
        """Return Drive search-result dicts (each carrying an extractable doc id)."""
        ...

    def docs_info(self, *, account: str, doc_id: str) -> dict[str, Any]:
        """Return Docs metadata for ``doc_id``."""
        ...

    def drive_get(self, *, account: str, doc_id: str) -> dict[str, Any]:
        """Return Drive file metadata for ``doc_id``."""
        ...

    def docs_cat(self, *, account: str, doc_id: str, max_bytes: int, all_tabs: bool) -> str:
        """Return the plain-text content of the Doc ``doc_id``."""
        ...


class GogSource:
    """Backend backed by the OpenClaw ``gog`` CLI.

    A thin delegating wrapper over the existing ``_gog_*`` helpers in
    ``gmail_threads`` / ``google_docs``; the subprocess transport, rate-limit
    retry and gog-shape normalization stay in those modules untouched, so this
    is behavior-preserving. The delegated imports are function-local because
    those modules import this one — a module-level import would cycle.
    """

    name = "gog"

    def search_threads(self, *, account: str, query: str, max_threads: int | None) -> list[dict[str, Any]]:
        from app.ingestion.gmail_threads import _gog_search_threads

        return _gog_search_threads(account=account, query=query, max_threads=max_threads)

    def get_thread(self, *, account: str, thread_id: str) -> dict[str, Any]:
        from app.ingestion.gmail_threads import _gog_get_thread

        return _gog_get_thread(account=account, thread_id=thread_id)

    def drive_search(self, *, account: str, query: str, max_docs: int | None, raw_query: bool) -> list[dict[str, Any]]:
        from app.ingestion.google_docs import _gog_drive_search

        return _gog_drive_search(account=account, query=query, max_docs=max_docs, raw_query=raw_query)

    def docs_info(self, *, account: str, doc_id: str) -> dict[str, Any]:
        from app.ingestion.google_docs import _gog_docs_info

        return _gog_docs_info(account=account, doc_id=doc_id)

    def drive_get(self, *, account: str, doc_id: str) -> dict[str, Any]:
        from app.ingestion.google_docs import _gog_drive_get

        return _gog_drive_get(account=account, doc_id=doc_id)

    def docs_cat(self, *, account: str, doc_id: str, max_bytes: int, all_tabs: bool) -> str:
        from app.ingestion.google_docs import _gog_docs_cat

        return _gog_docs_cat(account=account, doc_id=doc_id, max_bytes=max_bytes, all_tabs=all_tabs)


def get_google_source(backend: str | None = None) -> GoogleWorkspaceSource:
    """Return the configured Google Workspace ingestion backend.

    ``backend`` overrides the configured value (handy for tests). When omitted
    it reads ``ingestion.google_backend`` (default ``gog``). The ``gws`` and
    ``native`` backends are reserved but not wired up yet and raise
    :class:`NotImplementedError`; an unrecognized explicit override raises
    :class:`ValueError`.
    """
    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name == "gog":
        return GogSource()
    if name == "gws":
        raise NotImplementedError(
            "The 'gws' (Google Workspace CLI) ingestion backend is not wired up yet. "
            "Set `ingestion.google_backend: gog` for now."
        )
    if name == "native":
        raise NotImplementedError(
            "The 'native' (Google API) ingestion backend is not wired up yet. "
            "Set `ingestion.google_backend: gog` for now."
        )
    raise ValueError(
        f"Unknown ingestion.google_backend {name!r}; expected one of {', '.join(SUPPORTED_BACKENDS)}."
    )
