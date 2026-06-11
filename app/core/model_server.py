"""Warm local-model server — load Qwen + the LoRA adapter once, serve over HTTP.

Local generation otherwise spawns a fresh ``mlx_lm generate`` per draft, paying
the ~3s model load every time — fine for one draft, painful for a batch and for
streaming's first token. This wraps ``mlx_lm.server`` (an OpenAI-compatible HTTP
server) so the model is loaded once and reused: every local draft becomes a fast
HTTP call, which is what makes batch-on-local viable.

Scope notes:
  * The server loads a single ``--adapter-path`` at startup — the **global**
    adapter. Per-persona routing still uses the per-request subprocess path.
  * Pure plumbing here: nothing auto-starts unless a caller opts in. Wiring the
    generation paths to prefer it (with graceful fallback) is layered on top.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import httpx

from app.core.config import get_base_model
from app.core.settings import get_adapter_path

# Module-global handle to the managed server process + a lock so concurrent
# requests don't race to start (or kill) it. _started_adapter_sig records the
# adapter the running server loaded, so we can detect a retrain and reload.
_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_started_adapter_sig: float | None = None
# b242: refuse to spawn once shutdown has begun — without this, the prewarm
# thread could spawn the ~3GB child AFTER lifespan's stop() ran, leaving an
# orphan (own session) on the port past process exit.
_shutting_down = False
# b242: after repeated startup timeouts, fast-fail instead of kill/respawn
# churn — a model that legitimately needs longer than startup_timeout (cold
# disk, swap pressure) would otherwise be killed at the deadline forever,
# each cycle burning a full model load, and every stacked caller paying its
# own full timeout under the lock.
_consecutive_startup_timeouts = 0
_respawn_blocked_until = 0.0
_RESPAWN_COOLDOWN_SECONDS = 300.0


def _safetensors_ok(path) -> bool:
    """Cheap structural validity check for a ``.safetensors`` file — its 8-byte
    little-endian header length, a JSON-parseable header, AND the file being
    large enough to contain every tensor's recorded data range — WITHOUT
    loading mlx. A killed / disk-full / sleep-mid-train finetune can leave a
    truncated adapters.safetensors in the live dir; this keeps the warm server
    from loading it (and wedging all drafting) — it falls back to the base
    model instead (b163). The data-offsets bound (b242) catches files truncated
    mid-tensor-data, which pass the header check alone."""
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with open(path, "rb") as f:
            n = int.from_bytes(f.read(8), "little")
            if n <= 0 or 8 + n > size:
                return False
            header = f.read(n)
        parsed = json.loads(header)  # the header must be valid JSON
        if not isinstance(parsed, dict):
            return False
        max_end = 0
        for key, val in parsed.items():
            if key == "__metadata__":
                continue
            offs = val.get("data_offsets") if isinstance(val, dict) else None
            if not (isinstance(offs, (list, tuple)) and len(offs) == 2):
                return False
            max_end = max(max_end, int(offs[1]))
        if 8 + n + max_end > size:
            return False  # truncated mid-tensor-data
        return True
    except (OSError, ValueError, TypeError):
        return False


def _adapter_sig() -> float | None:
    """A signature (mtime) of the global adapter, or None if untrained/corrupt."""
    a = get_adapter_path() / "adapters.safetensors"
    try:
        return a.stat().st_mtime if (a.exists() and _safetensors_ok(a)) else None
    except OSError:
        return None


def get_server_config() -> dict:
    """``model.server`` config: enabled (default off) + port."""
    from app.core.config import load_config

    cfg = load_config() or {}
    model = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    srv = model.get("server", {}) if isinstance(model, dict) else {}
    srv = srv if isinstance(srv, dict) else {}
    return {"enabled": bool(srv.get("enabled", True)), "port": int(srv.get("port", 8088))}


# b174: Qwen3-4B-Instruct-2507 recommended sampling defaults + a sane max-tokens
# cap for the warm server. These set the SERVER's per-request defaults (applied
# only when a draft request omits the field); eval forces temp=0 + seed per
# request and so is unaffected. Overridable via ``model.server`` config.
_DEFAULT_TEMP = 0.7
_DEFAULT_TOP_P = 0.8
_DEFAULT_TOP_K = 20
_DEFAULT_MIN_P = 0.0
_DEFAULT_MAX_TOKENS = 1024


def _server_launch_args() -> list[str]:
    """Extra ``mlx_lm.server`` flags: Qwen3 default sampling + a max-tokens cap.

    Read from ``model.server`` config so they're tunable without code changes,
    falling back to the Qwen3-4B recommendations. Bounding ``--max-tokens``
    keeps a runaway decode from growing the KV cache unbounded on a 16GB box;
    Qwen3-4B's native context is 262K, far more than an email reply needs."""
    from app.core.config import load_config

    cfg = load_config() or {}
    model = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    srv = model.get("server", {}) if isinstance(model, dict) else {}
    srv = srv if isinstance(srv, dict) else {}

    def _num(key, default):
        try:
            v = srv.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    args = [
        "--temp", str(_num("temp", _DEFAULT_TEMP)),
        "--top-p", str(_num("top_p", _DEFAULT_TOP_P)),
        "--top-k", str(int(_num("top_k", _DEFAULT_TOP_K))),
        "--min-p", str(_num("min_p", _DEFAULT_MIN_P)),
        "--max-tokens", str(int(_num("max_tokens", _DEFAULT_MAX_TOKENS))),
    ]
    return args


def is_enabled() -> bool:
    return get_server_config()["enabled"]


def _port() -> int:
    return get_server_config()["port"]


def _base_url() -> str:
    return f"http://127.0.0.1:{_port()}"


def _adapter_base_matches() -> bool:
    """True if the global adapter's recorded base model matches the configured
    base — or if no base is recorded (legacy adapter, can't prove a mismatch).

    A LoRA adapter is bound to the exact base it was trained against: serving a
    Qwen2.5-1.5B adapter on a Qwen3-4B base errors or produces garbage. The
    train/promote path records ``base_model`` in the adapter's meta.json
    (finetune_lora._write_meta); here we read it back and refuse the adapter when
    it disagrees with the currently-configured base (b174). A missing/unreadable
    meta is treated as a legacy adapter and allowed — the structural
    safetensors check (b163) is the floor; this is the cross-base guard layered
    on top so the changeover never serves a stale-base adapter."""
    meta = get_adapter_path() / "meta.json"
    try:
        recorded = json.loads(meta.read_text(encoding="utf-8")).get("base_model")
    except (OSError, ValueError):
        return True  # no/garbage meta → legacy adapter, don't block on it
    if not recorded:
        return True
    return str(recorded) == str(get_base_model())


def _adapter_arg() -> str | None:
    """The global adapter path if one is trained, structurally valid (b163), AND
    trained against the currently-configured base (b174), else None (base model).

    Never hand mlx_lm.server a truncated adapter to choke on, and never hand it
    an adapter trained on a different base than the model it's loading — both
    would wedge/garble drafting. On a cross-base mismatch we fall back to
    base-model-only drafting and log it once so the operator knows a retrain is
    pending after a model migration."""
    adapter = get_adapter_path()
    a = adapter / "adapters.safetensors"
    if not (a.exists() and _safetensors_ok(a)):
        return None
    if not _adapter_base_matches():
        _log_base_mismatch_once()
        return None
    return str(adapter)


_warned_base_mismatch = False


def _log_base_mismatch_once() -> None:
    """Log the cross-base adapter skip exactly once per process (avoid spamming
    every request once a mismatch is in place)."""
    global _warned_base_mismatch
    if _warned_base_mismatch:
        return
    _warned_base_mismatch = True
    import logging

    logging.getLogger(__name__).warning(
        "adapter base model does not match configured base (%s); serving "
        "base-model-only drafts until a retrain produces a matching adapter",
        get_base_model(),
    )


def model_label() -> str:
    """The model_used label a server-produced draft should report — derived from
    the configured base model so it tracks a model migration (b174)."""
    from app.core.config import model_label as _label

    return _label(get_base_model(), with_adapter=bool(_adapter_arg()))


def is_healthy(*, timeout: float = 0.5) -> bool:
    """True if the server answers /health. Cheap enough to gate every request."""
    try:
        r = httpx.get(f"{_base_url()}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _pidfile():
    from app.core.settings import get_var_dir

    return get_var_dir() / "model_server.pid"


def _write_pidfile(proc: subprocess.Popen) -> None:
    """Record the managed child so a LATER process can find and kill it (b242).

    The child runs in its own session (start_new_session=True) and survives a
    parent crash/SIGKILL. Without this record, the restarted parent sees a
    healthy server it didn't spawn and can neither reload it after a retrain
    (it keeps serving the pre-crash adapter weights, while the sig-stamp claims
    otherwise) nor stop it; the CLI's stop/restart had the same blindness."""
    try:
        from app.core.atomic_io import atomic_write_json

        atomic_write_json(_pidfile(), {"pid": proc.pid, "port": _port()})
    except Exception:
        # Best-effort record; never let bookkeeping break the spawn (also
        # tolerates the fake procs the test suite injects).
        pass


def _clear_pidfile() -> None:
    try:
        _pidfile().unlink(missing_ok=True)
    except OSError:
        pass


def _kill_recorded_server() -> None:
    """Kill the server recorded in the pidfile, if it is alive and still looks
    like an mlx_lm.server (PID-reuse guard). Cross-process companion to
    _reap_locked for children this process doesn't own (b242)."""
    try:
        rec = json.loads(_pidfile().read_text(encoding="utf-8"))
        pid = int(rec["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        out = subprocess.run(  # noqa: S603
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        out = ""
    if "mlx_lm.server" not in out:
        _clear_pidfile()  # stale record (pid gone or reused by something else)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Not our child — can't wait(); poll for exit, escalate to SIGKILL.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    _clear_pidfile()


def ensure_running(*, startup_timeout: float = 40.0) -> bool:
    """Start the server if it isn't already healthy; wait until it is.

    Returns True if the server is healthy (already, or after starting). Safe to
    call before every local request — it's a no-op fast path once warm. Any
    failure returns False so the caller can fall back to the subprocess/cloud
    path rather than erroring.
    """
    global _proc, _started_adapter_sig, _consecutive_startup_timeouts, _respawn_blocked_until
    # Never auto-spawn the heavy (~3GB) model server inside the test suite — many
    # tests exercise the generation path via TestClient and must not start a real
    # server. The server's own spawn tests clear this env var to test the logic.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return is_healthy()
    if _shutting_down:
        return False
    # Fast path: already healthy AND serving the current adapter.
    if is_healthy() and _adapter_sig() == _started_adapter_sig:
        return True
    with _lock:
        if _shutting_down:
            return False
        # Re-check UNDER the lock so concurrent post-retrain threads don't each
        # tear down + respawn the server: the first to win reloads and stamps
        # _started_adapter_sig; the rest see it current here and return (b159).
        if is_healthy() and _adapter_sig() == _started_adapter_sig:
            return True
        if time.monotonic() < _respawn_blocked_until:
            return False  # in cooldown after repeated startup timeouts (b242)
        # We own the (re)start. Reap any existing handle first — a stale-adapter
        # healthy server (reload), a wedged server (alive but never /health), or a
        # half-started dud. _reap_locked (NOT stop(), which would re-acquire the
        # held lock and deadlock) kills + reaps it so we spawn fresh.
        if _proc is not None:
            _reap_locked()
        elif is_healthy():
            # A healthy server THIS process didn't spawn: a previous
            # incarnation's child that survived a parent crash, or a CLI-started
            # one. It may be serving stale adapter weights and we can't reload
            # it through _proc — kill the recorded pid so our fresh spawn can
            # bind (b242). Without this, the duplicate spawn bind-fails while
            # the orphan answers /health, and the stale weights get re-stamped
            # as current forever.
            _kill_recorded_server()
        cmd = [sys.executable, "-m", "mlx_lm.server", "--model", get_base_model(), "--port", str(_port())]
        # b174: set the server's DEFAULT sampling to the Qwen3-4B recommended
        # values (temp=0.7, top_p=0.8, top_k=20, min_p=0). mlx_lm.server applies
        # these only when a request omits the field — deterministic eval (b166)
        # passes temperature=0 + seed per request and so overrides them, keeping
        # eval reproducible. Bound generation length too so a runaway decode on
        # the 16GB box can't grow the KV cache without limit (Qwen3 default
        # context is 262K; we never need that for an email reply).
        cmd.extend(_server_launch_args())
        # Capture the sig at the same instant the adapter arg is read: stamping
        # a re-read sig AFTER the health wait let a promotion that landed
        # mid-model-load get stamped as already-served — the server then kept
        # the OLD weights with no reload ever triggering (b242 TOCTOU).
        sig_at_spawn = _adapter_sig()
        adapter = _adapter_arg()
        if adapter:
            cmd.extend(["--adapter-path", adapter])
        # Keep the child's stderr (b242): a server that dies during startup —
        # bad adapter, OOM, port bind — was undiagnosable with DEVNULL. One
        # file per spawn (truncate) keeps it bounded.
        stderr_target = subprocess.DEVNULL
        stderr_fh = None
        try:
            log_path = _pidfile().parent / "model_server.stderr.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_fh = open(log_path, "wb")
            stderr_target = stderr_fh
        except OSError:
            pass
        try:
            _proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
                start_new_session=True,
            )
        except Exception:
            _proc = None
            return False
        finally:
            if stderr_fh is not None:
                stderr_fh.close()  # child holds its own dup
        _write_pidfile(_proc)
        # Poll /health until the model finishes loading.
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if _proc is not None and _proc.poll() is not None:
                _proc = None  # died during startup
                _clear_pidfile()
                if is_healthy():
                    logging.getLogger(__name__).warning(
                        "model server spawn died but an UNMANAGED server still "
                        "answers on port %s — refusing to adopt it (it may be "
                        "serving stale adapter weights); falling back to the "
                        "subprocess path. Stop the stray server to recover.",
                        _port(),
                    )
                return False
            if is_healthy(timeout=1.0):
                _started_adapter_sig = sig_at_spawn  # what THIS spawn loaded
                _consecutive_startup_timeouts = 0
                _respawn_blocked_until = 0.0
                return True
            time.sleep(0.5)
        # Startup timed out but the child is still ALIVE (booting too slowly, or
        # wedged and never answering /health). Reap it inline so the NEXT call
        # respawns fresh instead of re-skipping the spawn and blocking the full
        # timeout again forever (the wedged/partial-start leak, b159).
        _reap_locked()
        _consecutive_startup_timeouts += 1
        if _consecutive_startup_timeouts >= 2:
            _respawn_blocked_until = time.monotonic() + _RESPAWN_COOLDOWN_SECONDS
            logging.getLogger(__name__).warning(
                "model server failed to become healthy within %.0fs twice in a "
                "row; pausing respawns for %.0fs (callers fall back to the "
                "subprocess path). See var/model_server.stderr.log.",
                startup_timeout,
                _RESPAWN_COOLDOWN_SECONDS,
            )
    return False


def _reap_locked() -> None:
    """Kill (whole process group) and reap the current ``_proc``, then clear it.

    The CALLER MUST already hold ``_lock``. Used by stop() and by
    ensure_running's failure/reload paths — calling stop() from inside the lock
    would re-acquire the non-reentrant ``_lock`` and self-deadlock (b159).

    SIGTERM the group, wait() with a short deadline so the child is reaped; if it
    ignores SIGTERM, escalate to SIGKILL and wait again (mirrors the
    kill-then-communicate pattern in generation/service.py). Without the wait()
    the long-lived FastAPI parent accumulated a persistent zombie — or a ~3GB
    stray for a SIGTERM-ignoring worker — on every retrain/restart (b154)."""
    global _proc
    proc = _proc
    _proc = None
    if proc is None:
        return
    _clear_pidfile()  # the record points at the child we're about to kill (b242)
    if proc.poll() is not None:
        # Already exited — reap it so it doesn't linger as a zombie.
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # Worker ignored SIGTERM — escalate to SIGKILL on the whole group.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def stop() -> None:
    """Terminate the managed server (whole process group) AND reap it (b154).

    Also kills a server recorded in the pidfile that THIS process doesn't own
    (b242) — that makes ``youos model server stop``/``restart`` work from the
    CLI process, which previously no-op'd (its ``_proc`` is always None) while
    printing success, leaving an unmanaged orphan behind."""
    with _lock:
        _reap_locked()
        _kill_recorded_server()


def shutdown() -> None:
    """stop() for process exit: additionally refuse any future spawn (b242).

    The lifespan teardown can win the race against the prewarm daemon thread —
    stop() then no-ops (nothing spawned yet) and the prewarm thread spawns the
    ~3GB child AFTER teardown, orphaning it past process exit. The flag is set
    BEFORE taking the lock so a prewarm already inside ensure_running rechecks
    it under the lock and refuses."""
    global _shutting_down
    _shutting_down = True
    stop()


def restart() -> bool:
    """Stop and start fresh — used after fine-tuning so the new adapter loads."""
    stop()
    return ensure_running()


def _payload(
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None,
    top_p: float | None,
    stream: bool,
    seed: int | None = None,
) -> dict:
    body: dict = {"prompt": prompt, "max_tokens": max_tokens, "stream": stream}
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    # b166: pin the PRNG for reproducible eval. mlx_lm.server (the warm server)
    # accepts an OpenAI-style ``seed`` field; unknown fields are ignored by the
    # server, and the eval also forces temperature=0 (greedy/argmax), so the
    # output is deterministic even if a given server build ignores ``seed``.
    if seed is not None:
        body["seed"] = seed
    return body


def complete(
    prompt: str,
    *,
    max_tokens: int = 300,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
    timeout: float = 120.0,
) -> str:
    """One-shot completion via the warm server. Raises on transport/HTTP error."""
    r = httpx.post(
        f"{_base_url()}/v1/completions",
        json=_payload(prompt, max_tokens=max_tokens, temperature=temperature, top_p=top_p, stream=False, seed=seed),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["text"]


def chat_complete(
    messages: list[dict],
    *,
    max_tokens: int = 300,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    seed: int | None = None,
    stop: list[str] | None = None,
    timeout: float = 120.0,
) -> str:
    """Chat completion via the warm server's /v1/chat/completions endpoint (b173).

    Using the chat endpoint makes mlx_lm.server apply the model's chat template
    to ``messages`` (matching how the adapter was fine-tuned). ``stop``
    (including ``<|im_end|>``) halts generation at the end of the assistant
    turn so the model can't run on into a fabricated bracket document. Raises on
    transport/HTTP error.
    """
    body: dict = {"messages": list(messages), "max_tokens": max_tokens, "stream": False}
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    # b174: Qwen3-4B recommends top_k=20 sampling. mlx_lm.server accepts an
    # OpenAI-style ``top_k`` field; unknown fields are ignored by older server
    # builds, so this is safe to always send when configured.
    if top_k is not None:
        body["top_k"] = top_k
    # b166: pin the PRNG for reproducible eval (see _payload for rationale).
    if seed is not None:
        body["seed"] = seed
    if stop:
        body["stop"] = list(stop)
    r = httpx.post(f"{_base_url()}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def stream(
    prompt: str,
    *,
    max_tokens: int = 400,
    temperature: float | None = None,
    top_p: float | None = None,
    timeout: float = 120.0,
) -> Iterator[str]:
    """Yield text deltas from the warm server's streaming completion endpoint.

    mlx_lm.server emits OpenAI-style ``data: {json}`` SSE lines whose
    ``choices[0].text`` is the incremental delta; a ``data: [DONE]`` sentinel (if
    sent) and any non-JSON line are ignored.
    """
    with httpx.stream(
        "POST",
        f"{_base_url()}/v1/completions",
        json=_payload(prompt, max_tokens=max_tokens, temperature=temperature, top_p=top_p, stream=True),
        timeout=timeout,
    ) as r:
        r.raise_for_status()
        dropped = 0
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("text", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                # A malformed chunk is a dropped token in the streamed draft.
                # Don't crash the stream, but don't vanish silently either —
                # count and report once so an incomplete stream is diagnosable.
                dropped += 1
                continue
            if delta:
                yield delta
        if dropped:
            logging.getLogger(__name__).debug(
                "stream: dropped %d unparseable SSE chunk(s) from warm server", dropped
            )
