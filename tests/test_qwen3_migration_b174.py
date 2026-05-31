"""b174 migration: local drafting base Qwen2.5-1.5B to Qwen3-4B-Instruct-2507.

Hermetic coverage of the source/config changes (NOT the operational retrain):
the repo-default base resolves to Qwen3-4B; the telemetry label helper derives
qwen3-4b-lora/-base from the configured base; the adapter-vs-base mismatch guard
refuses an adapter whose recorded base disagrees with the current base, allows a
matching one, and tolerates a legacy (no-meta) adapter; the warm server launches
with the Qwen3 recommended default sampling while deterministic eval (b166) still
forces temperature=0 + seed per request and so is unaffected.
"""

from __future__ import annotations

import importlib
import json

from app.core import config as cfg

QWEN3 = "Qwen/Qwen3-4B-Instruct-2507"
QWEN25 = "Qwen/Qwen2.5-1.5B-Instruct"


def test_repo_default_base_is_qwen3():
    assert cfg.DEFAULT_BASE_MODEL == QWEN3
    # A truthy config with no model.base falls back to the repo default. (An
    # empty {} is falsy, so get_base_model would reload the on-disk dev config,
    # which may still pin an old base pending the operational config update.)
    assert cfg.get_base_model({"user": {"name": "x"}}) == QWEN3


def test_config_base_overrides_default():
    assert cfg.get_base_model({"model": {"base": "some/other-model"}}) == "some/other-model"


def test_label_helper_derives_qwen3_labels():
    assert cfg.model_label(QWEN3, with_adapter=True) == "qwen3-4b-lora"
    assert cfg.model_label(QWEN3, with_adapter=False) == "qwen3-4b-base"


def test_label_helper_tracks_old_base_too():
    assert cfg.model_label(QWEN25, with_adapter=True) == "qwen2.5-1.5b-lora"
    assert cfg.model_label(QWEN25, with_adapter=False) == "qwen2.5-1.5b-base"


def test_label_helper_is_lora_bucketable():
    from app.core.stats import _classify_model_used

    assert _classify_model_used(cfg.model_label(QWEN3, with_adapter=True)) == "lora"
    assert _classify_model_used(cfg.model_label(QWEN3, with_adapter=False)) == "base"


def test_short_model_name_handles_4bit_and_empty():
    assert cfg._short_model_name("mlx-community/Qwen3-4B-Instruct-2507-4bit") == "qwen3-4b"
    assert cfg._short_model_name("Qwen/Qwen3-4B-Instruct-2507") == "qwen3-4b"
    assert cfg._short_model_name("") == "model"


def _make_adapter(tmp_path, *, base_in_meta):
    adir = tmp_path / "adapter"
    adir.mkdir(exist_ok=True)
    blob = b'{"x":{}}'
    hdr = len(blob).to_bytes(8, "little")
    (adir / "adapters.safetensors").write_bytes(hdr + blob)
    if base_in_meta is not None:
        (adir / "meta.json").write_text(json.dumps({"base_model": base_in_meta}))
    return adir


def _reload_server():
    import app.core.model_server as m

    importlib.reload(m)
    return m


def test_guard_refuses_cross_base_adapter(tmp_path, monkeypatch):
    srv = _reload_server()
    adir = _make_adapter(tmp_path, base_in_meta=QWEN25)
    monkeypatch.setattr(srv, "get_adapter_path", lambda: adir)
    monkeypatch.setattr(srv, "get_base_model", lambda: QWEN3)
    assert srv._adapter_base_matches() is False
    assert srv._adapter_arg() is None
    assert srv.model_label() == "qwen3-4b-base"


def test_guard_allows_matching_base_adapter(tmp_path, monkeypatch):
    srv = _reload_server()
    adir = _make_adapter(tmp_path, base_in_meta=QWEN3)
    monkeypatch.setattr(srv, "get_adapter_path", lambda: adir)
    monkeypatch.setattr(srv, "get_base_model", lambda: QWEN3)
    assert srv._adapter_base_matches() is True
    assert srv._adapter_arg() == str(adir)
    assert srv.model_label() == "qwen3-4b-lora"


def test_guard_allows_legacy_adapter_without_meta(tmp_path, monkeypatch):
    srv = _reload_server()
    adir = _make_adapter(tmp_path, base_in_meta=None)
    monkeypatch.setattr(srv, "get_adapter_path", lambda: adir)
    monkeypatch.setattr(srv, "get_base_model", lambda: QWEN3)
    assert srv._adapter_base_matches() is True
    assert srv._adapter_arg() == str(adir)


def test_server_launch_uses_qwen3_decoding_defaults(monkeypatch):
    srv = _reload_server()
    # _server_launch_args imports load_config from app.core.config locally, so
    # patch it at the source module (not the model_server namespace).
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    args = srv._server_launch_args()

    def _val(flag):
        return args[args.index(flag) + 1]

    assert _val("--temp") == "0.7"
    assert _val("--top-p") == "0.8"
    assert _val("--top-k") == "20"
    assert _val("--min-p") == "0.0"
    assert int(_val("--max-tokens")) <= 32768


def test_server_launch_args_overridable_via_config(monkeypatch):
    srv = _reload_server()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"model": {"server": {"temp": 0.3, "top_k": 5, "max_tokens": 256}}},
    )
    args = srv._server_launch_args()
    assert args[args.index("--temp") + 1] == "0.3"
    assert args[args.index("--top-k") + 1] == "5"
    assert args[args.index("--max-tokens") + 1] == "256"


def test_chat_complete_threads_top_k(monkeypatch):
    srv = _reload_server()
    monkeypatch.setattr(srv, "get_server_config", lambda: {"enabled": True, "port": 8088})

    captured = {}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _R()

    monkeypatch.setattr(srv.httpx, "post", fake_post)

    srv.chat_complete([{"role": "user", "content": "hi"}], top_k=20)
    assert captured["json"]["top_k"] == 20

    srv.chat_complete([{"role": "user", "content": "hi"}])
    assert "top_k" not in captured["json"]


def test_eval_determinism_b166_resolve_decoding_unchanged(monkeypatch):
    import app.generation.service as svc

    # _resolve_decoding imports load_config from app.core.config locally.
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    assert svc._resolve_decoding("scheduling", "high") == (None, None)
