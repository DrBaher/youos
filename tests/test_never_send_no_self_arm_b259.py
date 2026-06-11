"""b259: the never-send invariant is tamper-proof against a network config-write.

Pass-10 orchestrator audit F1 (HIGH design): the send gates themselves are
solid (probe-verified — nothing sends with sends off), but a token-authed
orchestrator could flip agent.send.enabled via POST /api/config/set (the
flags were network-writable) and then send. Tokens are all-or-nothing, so
the send frontier was only as safe as every token. Now the network path
refuses the frontier flags; CLI / config-file edits still work.
"""

from __future__ import annotations

import pytest

from app.core.feature_flags import (
    SEND_FRONTIER_FLAGS,
    SendFrontierWriteError,
    list_flags,
    set_flag,
)


def test_frontier_flags_refused_on_network_path(tmp_path):
    cfg = tmp_path / "youos_config.yaml"
    for key in SEND_FRONTIER_FLAGS:
        with pytest.raises(SendFrontierWriteError):
            set_flag(key, True, config_path=cfg, allow_send_frontier=False)
    assert not cfg.exists()  # nothing was written


def test_frontier_flags_still_settable_locally(tmp_path):
    cfg = tmp_path / "youos_config.yaml"
    # CLI/config-file path (the default) keeps working.
    assert set_flag("agent.send.enabled", True, config_path=cfg) is True
    assert set_flag("agent.outbound_kill_switch", True, config_path=cfg) is True


def test_non_frontier_flag_unaffected_on_network_path(tmp_path):
    cfg = tmp_path / "youos_config.yaml"
    val = set_flag(
        "generation.multi_candidate.enabled", True, config_path=cfg, allow_send_frontier=False
    )
    assert val is True


def test_list_flags_marks_frontier_locked():
    by_key = {f["key"]: f for f in list_flags({})}
    assert by_key["agent.send.enabled"]["network_locked"] is True
    assert by_key["generation.multi_candidate.enabled"]["network_locked"] is False


def test_api_config_set_refuses_frontier_flag(monkeypatch, tmp_path):
    """End-to-end through the route: a frontier flag returns 403, a normal
    flag still saves."""
    from fastapi.testclient import TestClient

    import app.core.config as config_mod
    from app.main import app

    cfg = tmp_path / "youos_config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    client = TestClient(app)

    r = client.post("/api/config/set", json={"key": "agent.send.enabled", "value": True})
    assert r.status_code == 403
    assert "send frontier" in r.json()["detail"]

    r = client.post(
        "/api/config/set", json={"key": "generation.multi_candidate.enabled", "value": True}
    )
    assert r.status_code == 200
