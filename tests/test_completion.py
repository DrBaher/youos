"""Shared model-tier selector used by the NL authoring helpers + digest summary."""

from __future__ import annotations

from app.core.completion import select_completion


def test_local_tier_uses_warm_server_when_enabled(monkeypatch):
    import app.core.model_server as ms

    monkeypatch.setattr(ms, "is_enabled", lambda: True)
    monkeypatch.setattr(ms, "complete", lambda p, **k: "LOCAL:" + p)
    fn = select_completion("local", max_tokens=10)
    assert fn is not None and fn("hi") == "LOCAL:hi"


def test_local_tier_none_when_warm_server_disabled(monkeypatch):
    import app.core.model_server as ms

    monkeypatch.setattr(ms, "is_enabled", lambda: False)
    assert select_completion("local", max_tokens=10) is None


def test_unknown_tier_falls_back_to_local(monkeypatch):
    import app.core.model_server as ms

    monkeypatch.setattr(ms, "is_enabled", lambda: True)
    monkeypatch.setattr(ms, "complete", lambda p, **k: "L")
    # anything that isn't 'cloud' is treated as local
    assert select_completion("gpt5", max_tokens=10) is not None


def test_cloud_tier_uses_claude_cli(monkeypatch):
    import app.generation.service as gen

    monkeypatch.setattr(gen, "_call_claude_cli", lambda p, **k: "CLOUD:" + p)
    fn = select_completion("cloud", max_tokens=10)
    assert fn is not None and fn("hi") == "CLOUD:hi"


def test_cloud_tier_threads_model_pin_and_timeout(monkeypatch):
    import app.generation.service as gen

    seen = {}

    def _spy(p, **kw):
        seen.update(kw)
        return p

    monkeypatch.setattr(gen, "_call_claude_cli", _spy)
    fn = select_completion("cloud", max_tokens=10, cloud_model="sonnet", timeout=300)
    fn("x")
    # the concrete model + timeout must reach the CLI, not the ambient default
    assert seen.get("model") == "sonnet"
    assert seen.get("timeout") == 300
