"""b173: inference must use the trained ChatML format, not a bracket blob.

Root cause fixed here: training wrote clean ChatML
(``{"messages":[{system},{user},{assistant}]}``) so the adapter learned to emit
a bare reply and stop at ``<|im_end|>``. Inference, however, built one raw
``[SYSTEM]…[EXEMPLARS]…[EXAMPLE i] Inbound:/Your reply:/--- …[TASK]…`` string and
sent it with NO stop sequence, so the model mimicked the bracket-document shape
and never halted. These tests lock in the alignment:

* drafting builds chat ``messages = [{system}, {user}]`` (no bracket scaffold),
* both the warm path and the subprocess path pass a ``<|im_end|>`` stop/eos,
* ``generate_draft`` still returns a ``DraftResponse``.
"""

from __future__ import annotations

import app.generation.service as svc
from app.core import model_server


# --------------------------------------------------------------------------
# 1. assemble_chat_messages builds [system, user] with no bracket scaffold.
# --------------------------------------------------------------------------
def test_assemble_chat_messages_shape():
    msgs = svc.assemble_chat_messages(
        inbound_message="Can we meet next Tuesday to review the budget?",
        reply_pairs=[],
        persona={"style": {"voice": "warm, concise", "avg_reply_words": 40}},
        prompts={"system_prompt": "You are BaherOS."},
        subject="Budget review",
        sender_type="colleague",
    )
    assert [m["role"] for m in msgs] == ["system", "user"]
    sys_content = msgs[0]["content"]
    user_content = msgs[1]["content"]

    # The bracket scaffold the broken model mimicked must be gone.
    for marker in ("[SYSTEM]", "[EXEMPLARS]", "[EXAMPLE", "[TASK]", "[REPLY]", "[INBOUND MESSAGE]"):
        assert marker not in sys_content
        assert marker not in user_content
    # The literal exemplar scaffold must not appear either.
    assert "Your reply:" not in sys_content
    assert "\n---\n" not in sys_content

    # Same information content as before lives in the system turn.
    assert "warm, concise" in sys_content
    assert "BaherOS" in sys_content
    # The inbound is the user turn.
    assert "next Tuesday" in user_content


def test_assemble_chat_messages_drops_exemplars():
    # Even with reply_pairs present, the per-exemplar Inbound:/Your reply:/---
    # block must not be re-emitted into the prompt (adapter encodes the voice).
    class _Match:
        inbound = "old inbound"
        reply = "old reply"
        score = 0.9

    msgs = svc.assemble_chat_messages(
        inbound_message="hi",
        reply_pairs=[_Match(), _Match()],
        persona={"style": {}},
        prompts={"system_prompt": "sys"},
    )
    sys_content = msgs[0]["content"]
    assert "old reply" not in sys_content
    assert "Your reply:" not in sys_content


# --------------------------------------------------------------------------
# 2. Stop / EOS plumbing.
# --------------------------------------------------------------------------
def test_chat_stop_sequences_include_eos():
    stops = svc._chat_stop_sequences()
    assert svc.EOS_TOKEN == "<|im_end|>"
    assert svc.EOS_TOKEN in stops
    # belt-and-suspenders scaffold guards
    assert "\n[" in stops
    assert "\nSubject:" in stops
    # caller-supplied extras are appended, deduped
    extra = svc._chat_stop_sequences(["FOO", svc.EOS_TOKEN])
    assert "FOO" in extra
    assert extra.count(svc.EOS_TOKEN) == 1


def test_split_chat_messages():
    st, ut = svc._split_chat_messages(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    )
    assert st == "S"
    assert ut == "U"


# --------------------------------------------------------------------------
# 3. Warm path uses /v1/chat/completions with a stop list including <|im_end|>.
# --------------------------------------------------------------------------
def test_warm_path_uses_chat_endpoint_with_stop(monkeypatch):
    seen = {}

    def fake_chat_complete(messages, **kw):
        seen["messages"] = messages
        seen["kw"] = kw
        return "Tuesday works for me."

    # adapter must be absent so the warm path is taken
    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(model_server, "is_enabled", lambda: True)
    monkeypatch.setattr(model_server, "ensure_running", lambda: True)
    monkeypatch.setattr(model_server, "chat_complete", fake_chat_complete)

    messages = [
        {"role": "system", "content": "You are BaherOS."},
        {"role": "user", "content": "Meet Tuesday?"},
    ]
    out = svc._call_local_model(messages, max_tokens=64, temperature=0.0, seed=1234)
    assert out == "Tuesday works for me."
    # routed the chat messages through, not a flattened bracket string
    assert seen["messages"] == messages
    stop = seen["kw"].get("stop")
    assert stop is not None
    assert svc.EOS_TOKEN in stop


# --------------------------------------------------------------------------
# 4. Subprocess path applies the chat template + --extra-eos-token <|im_end|>.
# --------------------------------------------------------------------------
def test_subprocess_path_chat_template_and_eos(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = "Tuesday works for me."
        stderr = ""

    def fake_run_subprocess(cmd, **kw):
        captured["cmd"] = cmd
        return _Result()

    # No warm server -> subprocess path. No adapter to keep it simple.
    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(model_server, "is_enabled", lambda: False)
    monkeypatch.setattr(svc, "_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "qwen-test")

    messages = [
        {"role": "system", "content": "You are BaherOS."},
        {"role": "user", "content": "Meet Tuesday?"},
    ]
    out = svc._call_local_model(messages, max_tokens=64, temperature=0.0, seed=1234)
    assert out == "Tuesday works for me."

    cmd = captured["cmd"]
    assert "generate" in cmd
    # chat template must be applied -> NO --ignore-chat-template
    assert "--ignore-chat-template" not in cmd
    # system + user passed via the supported flags
    assert "--system-prompt" in cmd
    assert "--prompt" in cmd
    # stop at end-of-turn
    assert "--extra-eos-token" in cmd
    eos_idx = cmd.index("--extra-eos-token")
    assert cmd[eos_idx + 1] == svc.EOS_TOKEN
    # determinism flags preserved (b166)
    assert "--seed" in cmd


# --------------------------------------------------------------------------
# 5. Raw-string callers (subject helper) still work and still stop at EOS.
# --------------------------------------------------------------------------
def test_raw_prompt_path_still_supported(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = "Budget review"
        stderr = ""

    def fake_run_subprocess(cmd, **kw):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(model_server, "is_enabled", lambda: False)
    monkeypatch.setattr(svc, "_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "qwen-test")

    out = svc._call_local_model("Suggest a subject line.", max_tokens=30, use_adapter=False)
    assert out == "Budget review"
    cmd = captured["cmd"]
    assert "--prompt" in cmd
    # raw completion path does not inject a system prompt
    assert "--system-prompt" not in cmd
