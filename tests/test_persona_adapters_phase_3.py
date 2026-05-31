"""Per-persona adapters — Phase 3: routed generation (opt-in, default-off).

Phase 1 (PR #28) added the schema and classified incoming feedback.
Phase 2 (PR #29) added the per-cohort training step. Phase 3 wires
the routing in `generate_draft`:

- `personas.routing_enabled: false` (default) → behavior is exactly
  what it was pre-Phase-3: global adapter when present, base model
  otherwise. Zero behavior change for any existing install.
- `personas.routing_enabled: true` AND a per-persona adapter exists
  for the inbound's `sender_type` → use the persona adapter,
  `model_used` reports `qwen2.5-1.5b-lora-<sender_type>`.
- Routing on but no persona adapter for this sender_type → falls
  through to the global adapter (Phase-2 trained both, or only
  global trained, or `sender_type` is "unknown").
- Routing on but no adapters trained at all → falls through to base
  model (and the doctor warns about the misconfig at startup).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import DEFAULT_BASE_MODEL, model_label

# b174: model_used labels are derived from the configured base model. These
# dispatch tests monkeypatch load_config to a dict WITHOUT model.base, so the
# service resolves the repo DEFAULT base — derive the expected label from the
# same default (not the on-disk dev config, which may still pin an old base).
_LORA = model_label(DEFAULT_BASE_MODEL, with_adapter=True)  # e.g. qwen3-4b-lora


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance(monkeypatch, tmp_path, _reset_settings):
    """YOUOS_DATA_DIR pointing at a tmp instance with the adapter dirs ready."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()
    return tmp_path


def _seed_persona_adapter(instance_root: Path, persona: str) -> Path:
    from app.core.settings import get_persona_adapter_path

    p = get_persona_adapter_path(persona)
    p.mkdir(parents=True, exist_ok=True)
    (p / "adapters.safetensors").write_bytes(b"weights")
    return p


def _seed_global_adapter(instance_root: Path) -> Path:
    from app.core.settings import get_adapter_path

    p = get_adapter_path()
    p.mkdir(parents=True, exist_ok=True)
    (p / "adapters.safetensors").write_bytes(b"weights")
    return p


# ── _persona_adapter_available helper ──────────────────────────────────────

def test_persona_adapter_available_returns_path_when_trained(instance):
    """A trained persona adapter resolves to its dir, not just True/False
    — the dispatch needs the path to pass to `--adapter-path`."""
    _seed_persona_adapter(instance, "internal")

    from app.generation.service import _persona_adapter_available

    result = _persona_adapter_available("internal")
    assert result is not None
    assert result.name == "internal"
    assert (result / "adapters.safetensors").exists()


def test_persona_adapter_available_returns_none_when_untrained(instance):
    from app.generation.service import _persona_adapter_available

    assert _persona_adapter_available("internal") is None


def test_persona_adapter_available_rejects_unknown_sender_type(instance):
    """Even with a directory present, `unknown` returns None — the
    prompt-side persona modes don't have a style anchor for it so per-
    persona routing makes no sense. Verified independently from whether
    a meta.safetensors was somehow seeded under `unknown`."""
    _seed_persona_adapter(instance, "unknown")
    from app.generation.service import _persona_adapter_available

    assert _persona_adapter_available("unknown") is None


def test_persona_adapter_available_handles_none_and_empty(instance):
    from app.generation.service import _persona_adapter_available

    assert _persona_adapter_available(None) is None
    assert _persona_adapter_available("") is None


# ── _persona_routing_enabled config gate ───────────────────────────────────

def test_routing_enabled_defaults_to_false(monkeypatch):
    """The whole point of opt-in: existing instances upgrade through
    Phases 1+2 with no generation behavior change."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **kw: {})
    from app.generation.service import _persona_routing_enabled

    assert _persona_routing_enabled() is False


def test_routing_enabled_reads_personas_routing_enabled(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config", lambda *a, **kw: {"personas": {"routing_enabled": True}},
    )
    from app.generation.service import _persona_routing_enabled

    assert _persona_routing_enabled() is True


def test_routing_enabled_tolerates_bad_config_shape(monkeypatch):
    """A fat-fingered scalar / non-dict under `personas` falls back to
    False instead of crashing the dispatch."""
    for bad in [{"personas": "yes"}, {"personas": ["a"]}, {"personas": 42}]:
        monkeypatch.setattr("app.core.config.load_config", lambda *a, b=bad, **kw: b)
        from app.generation.service import _persona_routing_enabled

        assert _persona_routing_enabled() is False


def test_routing_enabled_returns_false_when_load_config_raises(monkeypatch):
    """If config loading blows up (malformed YAML, etc.), routing stays
    off — better to use the global adapter than to crash generation."""
    def _boom(*a, **kw):
        raise RuntimeError("malformed YAML")

    monkeypatch.setattr("app.core.config.load_config", _boom)
    from app.generation.service import _persona_routing_enabled

    assert _persona_routing_enabled() is False


# ── _call_local_model: adapter_path override ──────────────────────────────

def test_call_local_model_uses_adapter_path_override(monkeypatch, instance):
    """When `adapter_path` is given, it's passed to `mlx_lm` regardless
    of `use_adapter`. Stub `_run_subprocess` so we observe the cmd
    without spawning anything."""
    import app.generation.service as svc

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = "==========\nhello\n==========\n"
        stderr = ""

    def _stub(cmd, *, timeout):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(svc, "_run_subprocess", _stub)

    custom = instance / "models" / "custom-adapter"
    svc._call_local_model("p", max_tokens=10, adapter_path=custom)
    assert "--adapter-path" in captured["cmd"]
    idx = captured["cmd"].index("--adapter-path")
    assert captured["cmd"][idx + 1] == str(custom)


def test_call_local_model_use_adapter_false_skips_adapter_flag(monkeypatch):
    """Back-compat with the existing `use_adapter=False` callers (the
    subject-generation path, the use_adapter=False review_queue
    comparison, the base-model retry on empty output)."""
    import app.generation.service as svc

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = "==========\nhello\n==========\n"
        stderr = ""

    monkeypatch.setattr(svc, "_run_subprocess", lambda cmd, timeout: (captured.update(cmd=cmd) or _R()))

    svc._call_local_model("p", max_tokens=10, use_adapter=False)
    assert "--adapter-path" not in captured["cmd"]


def test_call_local_model_default_uses_global_adapter(monkeypatch):
    """Pre-Phase-3 behavior: no `adapter_path`, `use_adapter=True` →
    falls back to the global ADAPTER_PATH constant."""
    import app.generation.service as svc

    # Force cold-subprocess path so the warm-server short-circuit doesn't
    # skip _run_subprocess (real mlx_lm.server may be up on the dev machine).
    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: False)

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = "==========\nhello\n==========\n"
        stderr = ""

    monkeypatch.setattr(svc, "_run_subprocess", lambda cmd, timeout: (captured.update(cmd=cmd) or _R()))

    svc._call_local_model("p", max_tokens=10)
    idx = captured["cmd"].index("--adapter-path")
    assert captured["cmd"][idx + 1] == str(svc.ADAPTER_PATH)


# ── End-to-end dispatch behavior ───────────────────────────────────────────

def _stub_generation_helpers(monkeypatch, *, sender, calls):
    """Stub everything generate_draft needs so we can exercise just the
    dispatch logic without a real DB / retrieval / prompts."""
    import app.generation.service as svc

    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None,
            documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {}, "modes": {}})
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    # Bypass DB connection setup.
    monkeypatch.setattr(svc, "_connect", lambda _p: __import__("sqlite3").connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: __import__("pathlib").Path("/tmp/x.db"))

    def _stub_call(prompt, *, max_tokens, use_adapter=True, adapter_path=None, **_kw):
        calls.append({"use_adapter": use_adapter, "adapter_path": adapter_path})
        return "this is a generated draft long enough to pass the empty-output retry"

    monkeypatch.setattr(svc, "_call_local_model", _stub_call)


def test_dispatch_routes_to_persona_adapter_when_enabled_and_trained(monkeypatch, instance):
    """Happy path: routing on + persona adapter exists → persona used.
    `model_used` carries the sender_type so the trace is honest."""
    persona_path = _seed_persona_adapter(instance, "internal")
    monkeypatch.setattr(
        "app.core.config.load_config", lambda *a, **kw: {"personas": {"routing_enabled": True}},
    )

    import app.generation.service as svc

    calls: list = []
    _stub_generation_helpers(monkeypatch, sender="colleague@x.com", calls=calls)
    # Force sender_type classification to "internal" for the test.
    monkeypatch.setattr(svc, "classify_sender", lambda _a: "internal")

    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", sender="colleague@x.com"),
        database_url="sqlite:///tmp",
        configs_dir=Path("/tmp"),
    )
    assert calls[0]["adapter_path"] == persona_path
    assert resp.model_used == f"{_LORA}-internal"


def test_dispatch_falls_through_to_global_when_persona_untrained(monkeypatch, instance):
    """Routing on but only the global is trained → use the global. The
    persona path returns None from `_persona_adapter_available`, falling
    through to the existing pre-Phase-3 dispatch."""
    monkeypatch.setattr(
        "app.core.config.load_config", lambda *a, **kw: {"personas": {"routing_enabled": True}},
    )

    import app.generation.service as svc

    calls: list = []
    _stub_generation_helpers(monkeypatch, sender="colleague@x.com", calls=calls)
    monkeypatch.setattr(svc, "classify_sender", lambda _a: "internal")
    # Force global adapter to "available" without depending on the
    # module-level ADAPTER_PATH (which captured at import time and points
    # at whatever dir the previous tests set up).
    monkeypatch.setattr(svc, "_adapter_available", lambda: True)

    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", sender="colleague@x.com"),
        database_url="sqlite:///tmp",
        configs_dir=Path("/tmp"),
    )
    # No adapter_path override; falls back to use_adapter=True path.
    assert calls[0]["adapter_path"] is None
    assert calls[0]["use_adapter"] is True
    assert resp.model_used == _LORA


def test_dispatch_routing_off_uses_global_even_with_persona_trained(monkeypatch, instance):
    """Default behavior: routing off → existing pre-Phase-3 dispatch.
    Critical for opt-in: an instance that just upgrades through Phase 3
    keeps using the global until the user explicitly flips the flag."""
    _seed_persona_adapter(instance, "internal")
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **kw: {})  # routing off

    import app.generation.service as svc

    calls: list = []
    _stub_generation_helpers(monkeypatch, sender="colleague@x.com", calls=calls)
    monkeypatch.setattr(svc, "classify_sender", lambda _a: "internal")
    monkeypatch.setattr(svc, "_adapter_available", lambda: True)

    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", sender="colleague@x.com"),
        database_url="sqlite:///tmp",
        configs_dir=Path("/tmp"),
    )
    # Used the global, not the persona adapter, despite the persona being trained.
    assert calls[0]["adapter_path"] is None
    assert resp.model_used == _LORA


def test_dispatch_routing_on_unknown_sender_falls_through_to_global(monkeypatch, instance):
    """When `sender_type=unknown` (no sender provided, or sender we
    couldn't classify), the persona resolver returns None and we
    correctly fall through. Avoids a "no adapter for unknown" crash."""
    monkeypatch.setattr(
        "app.core.config.load_config", lambda *a, **kw: {"personas": {"routing_enabled": True}},
    )

    import app.generation.service as svc

    calls: list = []
    _stub_generation_helpers(monkeypatch, sender=None, calls=calls)
    monkeypatch.setattr(svc, "_adapter_available", lambda: True)
    # No sender → sender_type_hint stays None.

    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi"),
        database_url="sqlite:///tmp",
        configs_dir=Path("/tmp"),
    )
    assert calls[0]["adapter_path"] is None
    assert resp.model_used == _LORA


def test_dispatch_use_adapter_false_bypasses_persona_routing(monkeypatch, instance):
    """`use_adapter=False` (the eval-comparison path, the base-model
    fallback) must skip persona routing too — otherwise the comparison
    is meaningless."""
    _seed_persona_adapter(instance, "internal")
    monkeypatch.setattr(
        "app.core.config.load_config", lambda *a, **kw: {"personas": {"routing_enabled": True}},
    )

    import app.generation.service as svc

    calls: list = []
    _stub_generation_helpers(monkeypatch, sender="colleague@x.com", calls=calls)
    monkeypatch.setattr(svc, "classify_sender", lambda _a: "internal")

    svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", sender="colleague@x.com", use_adapter=False),
        database_url="sqlite:///tmp",
        configs_dir=Path("/tmp"),
    )
    # use_adapter=False short-circuits both persona routing AND the
    # global adapter — the base model runs.
    assert calls[0]["adapter_path"] is None
    assert calls[0]["use_adapter"] is False


# ── Doctor warning ───────────────────────────────────────────────────────

def test_doctor_warns_when_routing_on_but_no_adapters_trained(monkeypatch, tmp_path, _reset_settings):
    """Catches the order-of-operations misconfig: user flips the flag
    before Phase 2's nightly has trained anything. Every draft would
    silently fall through to the global — surface the warning so they
    notice before debugging mysterious "why didn't routing change my
    output" sessions."""
    from app.core.settings import get_settings

    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()
    # Patch load_config rather than relying on disk reads — the
    # CONFIG_PATH constant captured at import time and tests further
    # downstream may have polluted it.
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"personas": {"routing_enabled": True}, "user": {"emails": ["a@b"]}},
    )
    get_settings.cache_clear()
    try:
        from app.core.doctor import run_doctor_checks_full

        _, _failures, warnings = run_doctor_checks_full()
        assert any("personas.routing_enabled" in w and "no per-persona adapters" in w for w in warnings), (
            f"expected persona routing warning in {warnings}"
        )
    finally:
        get_settings.cache_clear()


def test_doctor_does_not_warn_when_routing_off(monkeypatch, tmp_path, _reset_settings):
    """No warning when the flag is off — the absence of adapters is
    fine until the user opts in."""
    from app.core.settings import get_settings

    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"user": {"emails": ["a@b"]}},
    )
    get_settings.cache_clear()
    try:
        from app.core.doctor import run_doctor_checks_full

        _, _failures, warnings = run_doctor_checks_full()
        assert not any("personas.routing_enabled" in w for w in warnings)
    finally:
        get_settings.cache_clear()


def test_doctor_does_not_warn_when_routing_on_and_adapter_trained(monkeypatch, tmp_path, _reset_settings):
    """No warning once at least one persona adapter is trained — that's
    the working state and the warning would just be noise."""
    from app.core.settings import get_settings

    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    (tmp_path / "var").mkdir()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"personas": {"routing_enabled": True}, "user": {"emails": ["a@b"]}},
    )
    get_settings.cache_clear()
    try:
        # Train at least one persona.
        from app.core.settings import get_persona_adapter_path

        p = get_persona_adapter_path("internal")
        p.mkdir(parents=True)
        (p / "adapters.safetensors").write_bytes(b"weights")

        from app.core.doctor import run_doctor_checks_full

        _, _failures, warnings = run_doctor_checks_full()
        assert not any("personas.routing_enabled" in w for w in warnings)
    finally:
        get_settings.cache_clear()
