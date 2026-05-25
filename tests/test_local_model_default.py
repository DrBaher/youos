"""Local MLX model is the default for both draft and subject generation.

Guards the v0.1.25 behaviour: when `mlx_lm` is on PATH the local model is
used even without a LoRA adapter (base model), and subject generation does
not silently call the Claude CLI behind it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_local_model_available_true_when_mlx_on_path():
    from app.generation import service as svc

    with patch.object(svc.shutil, "which", return_value="/opt/homebrew/bin/mlx_lm"):
        assert svc._local_model_available() is True


def test_local_model_available_false_when_mlx_missing():
    from app.generation import service as svc

    with patch.object(svc.shutil, "which", return_value=None):
        assert svc._local_model_available() is False


def test_local_model_available_is_independent_of_adapter():
    """The point of v0.1.25: MLX usable without the LoRA adapter."""
    from app.generation import service as svc

    with (
        patch.object(svc.shutil, "which", return_value="/opt/homebrew/bin/mlx_lm"),
        patch.object(svc, "_adapter_available", return_value=False),
    ):
        assert svc._local_model_available() is True


def test_generate_subject_uses_local_model_when_available():
    """Subject generation must prefer MLX over the Claude CLI when MLX is present.

    Regression: `generate_subject` was hardcoded to `_call_claude_cli`, which on
    the nightly job stalled every benchmark case for 120s before timing out.
    """
    from app.generation import service as svc

    # Bypass the rule-based fallback so we exercise the model path.
    with (
        patch.object(svc, "_subject_fallback", return_value=None),
        patch.object(svc, "get_model_fallback", return_value="claude"),
        patch.object(svc, "_local_model_available", return_value=True),
        patch.object(svc, "_call_local_model", return_value="Project update") as call_local,
        patch.object(svc, "_call_claude_cli") as call_claude,
    ):
        result = svc.generate_subject(
            "Random body with no subject header.",
            "Thanks!",
            "sqlite:///test.db",
            Path("configs"),
        )

    assert result == "Project update"
    call_local.assert_called_once()
    call_claude.assert_not_called()
    # Subject prompts run the base model (no adapter) for speed.
    assert call_local.call_args.kwargs.get("use_adapter") is False


def test_generate_subject_falls_back_to_claude_when_mlx_missing():
    from app.generation import service as svc

    with (
        patch.object(svc, "_subject_fallback", return_value=None),
        patch.object(svc, "get_model_fallback", return_value="claude"),
        patch.object(svc, "_local_model_available", return_value=False),
        patch.object(svc, "_call_local_model") as call_local,
        patch.object(svc, "_call_claude_cli", return_value="Subject: Hi") as call_claude,
    ):
        result = svc.generate_subject(
            "Random body with no subject header.",
            "Thanks!",
            "sqlite:///test.db",
            Path("configs"),
        )

    assert result == "Hi"
    call_claude.assert_called_once()
    call_local.assert_not_called()


def test_generate_subject_skips_model_when_fallback_is_none():
    """`model_fallback: none` must not call any model — local or cloud."""
    from app.generation import service as svc

    with (
        patch.object(svc, "_subject_fallback", return_value=None),
        patch.object(svc, "get_model_fallback", return_value="none"),
        patch.object(svc, "_call_local_model") as call_local,
        patch.object(svc, "_call_claude_cli") as call_claude,
    ):
        result = svc.generate_subject(
            "No header here.",
            "Reply.",
            "sqlite:///test.db",
            Path("configs"),
        )

    assert result is None
    call_local.assert_not_called()
    call_claude.assert_not_called()
