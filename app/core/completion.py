"""Pick a completion function by model tier — shared by the NL authoring helpers.

Authoring translations (NL → Gmail query, NL → rule) and the digest summary all
need "give me a completion from the local warm model OR the frontier/cloud
model." This centralises that choice so the surfaces stay consistent.

'local' → the warm on-device model server (no egress). 'cloud' → the Claude CLI
(a frontier model). Returns None if the chosen tier is unavailable, so callers
fall back gracefully. The cloud path sends only what the caller passes it — for
the authoring helpers that's the user's own short description (a query phrase /
rule sentence), never their email content.
"""

from __future__ import annotations


def select_completion(model: str, *, max_tokens: int, temperature: float = 0.0):
    """Return a ``complete_fn(prompt) -> str`` for the given tier, or None if
    unavailable. ``model`` is 'cloud' for the frontier model; anything else
    (default) is the local warm model."""
    if str(model).strip().lower() == "cloud":
        try:
            from app.generation.service import _call_claude_cli
        except Exception:
            return None
        return lambda p: _call_claude_cli(p)
    from app.core import model_server

    if model_server.is_enabled():
        return lambda p: model_server.complete(p, max_tokens=max_tokens, temperature=temperature)
    return None
