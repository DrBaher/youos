"""Feature-flag core + `youos config` CLI (Config PR 1).

The whitelisted flag core is the single write path for the CLI, the settings
page, and the onboarding wizard. These pin get/set/list, type coercion, the
whitelist guard, and the CLI wiring.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.core import feature_flags as ff
from app.core.config import load_config

runner = CliRunner()


# --- core ------------------------------------------------------------------


def test_list_flags_defaults_on_empty_config():
    flags = ff.list_flags(config={})
    by_key = {f["key"]: f for f in flags}
    assert by_key["generation.multi_candidate.enabled"]["value"] is False
    assert by_key["generation.log_drafts"]["value"] is True  # default-on flag
    assert by_key["ingestion.google_backend"]["value"] == "gog"
    # Drafting controls surfaced in Settings (local-by-default + warm server).
    assert by_key["review.draft_model"]["value"] == "auto"
    assert by_key["model.server.enabled"]["value"] is True


def test_abstain_min_quality_is_whitelisted_and_clamped(tmp_path):
    """b276: the autonomous draft-abstain floor must be a first-class flag so it's
    settable via the config API / Settings, not only by hand-editing YAML. The
    key MUST match what generation._abstain_config reads."""
    by_key = {f["key"]: f for f in ff.list_flags(config={})}
    assert "generation.abstain.min_quality" in by_key
    assert by_key["generation.abstain.min_quality"]["value"] == 0.5  # product default
    cfg = tmp_path / "youos_config.yaml"
    # Set + clamp to range.
    assert ff.set_flag("generation.abstain.min_quality", "0.25", config_path=cfg) == 0.25
    assert load_config(cfg)["generation"]["abstain"]["min_quality"] == 0.25
    assert ff.set_flag("generation.abstain.min_quality", "1.7", config_path=cfg) == 1.0  # clamped


def test_get_flag_from_config_and_default():
    cfg = {"generation": {"multi_candidate": {"enabled": True}}}
    assert ff.get_flag("generation.multi_candidate.enabled", config=cfg) is True
    assert ff.get_flag("personas.routing_enabled", config=cfg) is False  # default


def test_set_flag_roundtrips_to_disk(tmp_path):
    cfg = tmp_path / "youos_config.yaml"
    assert ff.set_flag("generation.multi_candidate.enabled", "true", config_path=cfg) is True
    data = load_config(cfg)
    assert data["generation"]["multi_candidate"]["enabled"] is True


def test_set_flag_choice(tmp_path):
    cfg = tmp_path / "youos_config.yaml"
    assert ff.set_flag("ingestion.google_backend", "gws", config_path=cfg) == "gws"
    assert load_config(cfg)["ingestion"]["google_backend"] == "gws"


def test_set_flag_coercion_variants(tmp_path):
    cfg = tmp_path / "c.yaml"
    assert ff.set_flag("generation.log_drafts", "off", config_path=cfg) is False
    assert ff.set_flag("generation.log_drafts", "1", config_path=cfg) is True


def test_set_flag_unknown_key_raises():
    with pytest.raises(KeyError):
        ff.set_flag("generation.bogus", "true", config_path=None)


def test_set_flag_bad_bool_raises(tmp_path):
    with pytest.raises(ValueError, match="boolean"):
        ff.set_flag("generation.multi_candidate.enabled", "maybe", config_path=tmp_path / "c.yaml")


def test_set_flag_bad_choice_raises(tmp_path):
    with pytest.raises(ValueError, match="one of"):
        ff.set_flag("ingestion.google_backend", "outlook", config_path=tmp_path / "c.yaml")


def test_set_flag_does_not_clobber_siblings(tmp_path):
    cfg = tmp_path / "c.yaml"
    ff.set_flag("generation.repair.enforce_greeting_closing", "true", config_path=cfg)
    ff.set_flag("generation.repair.strip_trailing_signature", "true", config_path=cfg)
    rep = load_config(cfg)["generation"]["repair"]
    assert rep["enforce_greeting_closing"] is True and rep["strip_trailing_signature"] is True


# --- CLI -------------------------------------------------------------------


def test_cli_config_list_runs(monkeypatch):
    monkeypatch.setattr(
        ff, "list_flags",
        lambda config=None: [{"key": "generation.log_drafts", "label": "Log draft events", "type": "bool", "value": True}],
    )
    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "generation.log_drafts" in result.stdout


def test_cli_config_set_wires_args(monkeypatch):
    calls = {}
    monkeypatch.setattr(ff, "set_flag", lambda k, v: calls.update(k=k, v=v) or True)
    result = runner.invoke(app, ["config", "set", "generation.multi_candidate.enabled", "true"])
    assert result.exit_code == 0
    assert calls == {"k": "generation.multi_candidate.enabled", "v": "true"}
    assert "set generation.multi_candidate.enabled" in result.stdout


def test_cli_config_set_unknown_key_exits_nonzero(monkeypatch):
    def boom(k, v):
        raise KeyError(f"unknown flag {k!r}")

    monkeypatch.setattr(ff, "set_flag", boom)
    result = runner.invoke(app, ["config", "set", "nope", "true"])
    assert result.exit_code == 1


def test_cli_config_get_unknown_key_exits_nonzero(monkeypatch):
    def boom(k):
        raise KeyError(k)

    monkeypatch.setattr(ff, "get_flag", boom)
    result = runner.invoke(app, ["config", "get", "nope"])
    assert result.exit_code == 1


# --- b131: int flags must validate, not persist garbage that crashes reads ---


def test_set_flag_int_rejects_non_numeric(tmp_path):
    # A non-numeric int value used to fall through coerce_value and persist
    # verbatim; the scheduler's bare int() read then raised and killed the loop.
    # It must now raise ValueError -> the API returns 400, nothing is persisted.
    cfgp = tmp_path / "c.yaml"
    with pytest.raises(ValueError, match="integer"):
        ff.set_flag("agent.interval_minutes", "abc", config_path=cfgp)
    with pytest.raises(ValueError, match="integer"):
        ff.set_flag("agent.interval_minutes", "", config_path=cfgp)
    # bool is an int subclass but is not a valid flag int.
    with pytest.raises(ValueError, match="integer"):
        ff.set_flag("agent.interval_minutes", True, config_path=cfgp)
    # A valid numeric string still coerces to int and round-trips.
    assert ff.set_flag("agent.interval_minutes", "20", config_path=cfgp) == 20


def test_coerce_value_int_clamps_to_bounds():
    flag = {"type": "int", "min": 1, "max": 10}
    assert ff.coerce_value(flag, "0") == 1
    assert ff.coerce_value(flag, "99") == 10
    assert ff.coerce_value(flag, "5") == 5


# --- b140: server.pin is a credential, set via `config set-pin`, not a flag ---


def test_set_flag_server_pin_rejected_with_hint():
    with pytest.raises(KeyError, match="set-pin"):
        ff.set_flag("server.pin", "1234")


def test_cli_config_set_pin_writes_hashed(monkeypatch):
    import app.core.config as cfgmod
    from app.core.auth import verify_pin

    saved: dict = {}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda cfg, *a, **k: saved.update(cfg=cfg))
    result = runner.invoke(app, ["config", "set-pin", "1234"])
    assert result.exit_code == 0
    stored = saved["cfg"]["server"]["pin"]
    assert stored.startswith("pbkdf2:") and verify_pin("1234", stored)  # hashed, never plaintext


def test_cli_config_set_server_pin_flag_errors_with_hint():
    result = runner.invoke(app, ["config", "set", "server.pin", "1234"])
    assert result.exit_code == 1
    assert "set-pin" in result.output  # points at the working command
