import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.agent_routes import router as agent_router
from app.api.facts_routes import router as facts_router
from app.api.feedback_routes import _BOOKMARKLET_ROUTER as bookmarklet_router
from app.api.feedback_routes import router as feedback_router
from app.api.history_routes import router as history_router
from app.api.review_queue_routes import router as review_queue_router
from app.api.routes import router
from app.api.sender_routes import router as sender_router
from app.api.stats_routes import router as stats_router
from app.api.stream_routes import router as stream_router
from app.core.auth import (
    LoginRateLimiter,
    compute_allowed_origins,
    compute_token_allowed_origins,
    create_session_token,
    is_auth_enabled,
    load_sessions,
    persist_new_session,
    request_origin_allowed,
    token_request_origin_allowed,
    verify_api_token,
    verify_pin,
)
from app.core.config import load_config
from app.core.data_safety import run_startup_safety_checks, validate_instance_paths
from app.core.settings import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
SESSION_COOKIE = "youos_session"
SESSION_MAX_AGE = 86400  # 24 hours

# Reject a request body larger than this up front (defense-in-depth so every
# string/list field is bounded by default, not per-field whack-a-mole). The
# largest legitimate body is a few capped 50 KB text fields, well under this.
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB


def _host_allowed(request: Request, config: dict) -> bool:
    """True if the request's Host header is one we serve. Blocks DNS rebinding:
    a page on evil.com that re-resolves to 127.0.0.1 still sends Host=evil.com,
    so an unauthenticated (no-PIN) localhost API can't be driven from a remote
    page. Skipped for a bind-all host (can't enumerate; that's the exposed mode
    where a PIN + the Origin check + cookie scoping apply)."""
    from urllib.parse import urlsplit

    from app.core.config import get_server_host, get_tailscale_hostname

    server_host = (get_server_host(config) or "").strip().lower()
    if server_host in ("0.0.0.0", "::", ""):  # bind-all / unset → can't allowlist
        return True
    raw = request.headers.get("host", "")
    if not raw:
        return True  # no Host header → a non-browser client; browsers (the only
        # DNS-rebinding vector) always send Host, so a missing one isn't the threat
    host = (urlsplit(f"//{raw}").hostname or "").lower()
    allowed = {"127.0.0.1", "localhost", "::1", "testserver", server_host}  # testserver = Starlette TestClient
    tailscale = (get_tailscale_hostname(config) or "").strip().lower()
    if tailscale:
        allowed.add(tailscale)
    return host in allowed


class PinAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login when PIN is configured.

    Two Origin checks on state-changing requests:

    1. **Cookie path** — Origin/Referer must match
       ``compute_allowed_origins`` (defense-in-depth on top of
       ``SameSite=Lax``).
    2. **Token path** — if ``server.token_allowed_origins`` is
       configured, Origin must match that allowlist. When unconfigured
       (the default), token requests authenticate from any origin
       (preserves historical behaviour and back-compat with existing
       extension installs that haven't opted in yet).
    """

    SKIP_PREFIXES = ("/login", "/static")

    def __init__(self, app, config: dict | None = None, *, config_provider=None):
        super().__init__(app)
        # Resolve config live on every request so a PIN / origin allowlist set
        # after startup takes effect without a restart. Production passes
        # ``config_provider=load_config``; a static ``config`` dict (tests, or
        # an explicit snapshot) is wrapped as a frozen provider.
        if config_provider is not None:
            self._config_provider = config_provider
        elif config is not None:
            self._config_provider = lambda: config
        else:
            self._config_provider = load_config
        initial = self._config_provider()
        self.config = initial
        # Load persisted sessions (already pruned of expired tokens on load).
        # Keep the creation timestamps in memory so expiry can be enforced
        # server-side — storing only the keys let captured tokens replay
        # indefinitely until process restart, ignoring SESSION_MAX_AGE.
        self.sessions: dict[str, float] = dict(load_sessions())
        self.limiter = LoginRateLimiter()
        # Defaults for any external reader; dispatch recomputes per request
        # from the live config so a post-start change is honored.
        self.allowed_origins: set[str] = compute_allowed_origins(initial)
        self.token_allowed_origins: set[str] | None = compute_token_allowed_origins(initial)

    def _origin_check_passes(self, request: Request, allowed_origins: set[str]) -> bool:
        return request_origin_allowed(
            method=request.method,
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            allowed_origins=allowed_origins,
        )

    def _token_origin_check_passes(self, request: Request, token_allowed_origins: set[str] | None) -> bool:
        return token_request_origin_allowed(
            method=request.method,
            origin=request.headers.get("origin"),
            allowed_origins=token_allowed_origins,
        )

    async def dispatch(self, request: Request, call_next):
        # Reject an oversized body up front — bounds every string/list field by
        # default, regardless of per-field caps or auth state.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                oversized = int(content_length) > _MAX_BODY_BYTES
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
            if oversized:
                return JSONResponse({"detail": "request body too large"}, status_code=413)

        # Re-read config (lru-cached, cleared on every save_config) per request
        # rather than the snapshot captured at construction. Otherwise a user
        # who sets a PIN / origin allowlist on a network-reachable instance
        # stays UNAUTHENTICATED until the server restarts — a real exposure
        # window on the privacy-first product. The scheduler already re-reads
        # config each tick for the same reason.
        config = self._config_provider()
        # Reject a foreign Host (DNS rebinding) BEFORE the no-PIN short-circuit,
        # so a rebound evil.com page can't reach the unauthenticated API.
        if not _host_allowed(request, config):
            return JSONResponse({"detail": "host not allowed"}, status_code=421)
        if not is_auth_enabled(config):
            return await call_next(request)

        allowed_origins = compute_allowed_origins(config)
        token_allowed_origins = compute_token_allowed_origins(config)

        path = request.url.path
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if token:
            created_at = self.sessions.get(token)
            if created_at is not None:
                if time.time() - created_at < SESSION_MAX_AGE:
                    if not self._origin_check_passes(request, allowed_origins):
                        return JSONResponse(
                            {"detail": "origin not allowed"},
                            status_code=403,
                        )
                    return await call_next(request)
                # Expired — evict so it can't be reused.
                self.sessions.pop(token, None)

        # Non-cookie clients (the browser extension) authenticate with an API
        # token sent as `X-YouOS-Token` or `Authorization: Bearer <token>`.
        # Token auth is not CSRF-prone: an attacker can't make the browser
        # attach a token they don't already know. We do still check Origin
        # *when the user has configured an allowlist* — that narrows the
        # surface so a compromised page that exfiltrated the token can't
        # also reuse it from any origin. Default (no allowlist) preserves
        # the historical token-authenticates-anywhere behaviour.
        api_token = request.headers.get("x-youos-token")
        if not api_token:
            auth_header = request.headers.get("authorization", "")
            if auth_header[:7].lower() == "bearer ":
                api_token = auth_header[7:].strip()
        # verify_api_token runs PBKDF2 (per stored hash) — offload it to the
        # threadpool so a flood of bogus X-YouOS-Token headers can't block the
        # async event loop (one full PBKDF2 per request) and stall every other
        # in-flight request.
        if api_token and await run_in_threadpool(verify_api_token, api_token):
            if not self._token_origin_check_passes(request, token_allowed_origins):
                return JSONResponse(
                    {"detail": "origin not allowed for token auth"},
                    status_code=403,
                )
            return await call_next(request)

        return RedirectResponse(url="/login", status_code=303)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()

    # Run data safety checks at startup
    validate_instance_paths(settings)
    safety_report = run_startup_safety_checks(settings)
    if safety_report.warnings:
        for warning in safety_report.warnings:
            print(f"[YOUOS WARNING]: {warning}")
        # Optionally, block startup here if warnings are critical

    # Warn loudly if the server is reachable beyond the local machine without a
    # PIN. With no PIN, PinAuthMiddleware is a no-op and every endpoint is open.
    config = load_config()
    if not is_auth_enabled(config):
        from app.core.config import get_server_host, get_tailscale_hostname

        host = get_server_host(config)
        loopback = host in ("127.0.0.1", "localhost", "::1", "")
        if not loopback or get_tailscale_hostname(config):
            print(
                "[YOUOS SECURITY]: Server is reachable beyond localhost "
                f"(host={host or '0.0.0.0'}"
                + (", Tailscale enabled" if get_tailscale_hostname(config) else "")
                + ") but no PIN is set — the web UI and API are UNAUTHENTICATED. "
                "Set a PIN: run `youos config set-pin <PIN>` before exposing YouOS."
            )

    # Pre-warm the local model server (load the model once, off the request path)
    # so the first draft isn't slow. Background thread → never blocks startup; a
    # no-op when disabled or when mlx_lm is unavailable.
    try:
        from app.core import model_server

        if model_server.is_enabled():
            import threading

            threading.Thread(target=model_server.ensure_running, daemon=True).start()
    except Exception:
        pass

    # γ: background agent-triage loop. No-op under pytest; otherwise opts in
    # via `agent.enabled` (read every iteration so flipping the flag takes
    # effect without restart).
    try:
        from app.agent import scheduler as _agent_scheduler

        _agent_scheduler.start(app)
    except Exception as exc:
        # Failure to start the scheduler must not block server startup.
        print(f"[YOUOS WARNING]: agent scheduler failed to start: {exc}")

    yield

    # Stop the agent loop cleanly before everything else tears down.
    try:
        from app.agent import scheduler as _agent_scheduler

        await _agent_scheduler.stop(app)
    except Exception:
        pass

    # Clear embedding cache on shutdown
    from app.core.embeddings import clear_embedding_cache

    clear_embedding_cache()

    # Stop the managed model server so it doesn't outlive YouOS.
    try:
        from app.core import model_server

        model_server.stop()
    except Exception:
        pass


def create_app() -> FastAPI:
    settings = get_settings()
    config = load_config()
    instance_name = getattr(settings, "instance_name", "YouOS")
    if instance_name == "YouOS":
        instance_name = str(config.get("user", {}).get("display_name") or instance_name)

    app = FastAPI(
        title=f"{settings.app_name} ({instance_name})",
        version=settings.version,
        description="Your personal AI email copilot — learns your style from your Gmail history.",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.config = config

    auth_middleware = PinAuthMiddleware(app, config_provider=load_config)
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_middleware.dispatch)
    app.state.auth = auth_middleware

    # ── Login routes ──
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        # Re-read config so a PIN set after startup makes /login functional
        # (matches the middleware's per-request read).
        if not is_auth_enabled(load_config()):
            return RedirectResponse(url="/feedback", status_code=303)
        template = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("{{ error }}", ""))

    @app.post("/login")
    async def login_submit(request: Request):
        current_config = load_config()
        if not is_auth_enabled(current_config):
            return RedirectResponse(url="/feedback", status_code=303)

        client_ip = request.client.host if request.client else "unknown"
        if auth_middleware.limiter.is_locked(client_ip):
            template = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
            return HTMLResponse(
                template.replace("{{ error }}", "Too many attempts. Wait 60 seconds."),
                status_code=429,
            )

        form = await request.form()
        pin = form.get("pin", "")
        stored_hash = current_config.get("server", {}).get("pin", "")

        if verify_pin(str(pin), stored_hash):
            auth_middleware.limiter.reset(client_ip)
            token = create_session_token()
            auth_middleware.sessions[token] = time.time()
            persist_new_session(token)
            response = RedirectResponse(url="/feedback", status_code=303)
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
            return response

        auth_middleware.limiter.record_attempt(client_ip)
        template = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(
            template.replace("{{ error }}", "Incorrect PIN."),
            status_code=401,
        )

    app.include_router(router)
    app.include_router(feedback_router)
    app.include_router(sender_router)
    app.include_router(review_queue_router)
    app.include_router(stats_router)
    app.include_router(bookmarklet_router)
    app.include_router(stream_router)
    app.include_router(history_router)
    app.include_router(facts_router)
    app.include_router(agent_router)

    # Shared front-end assets (design system: youos.css + youos.js). The auth
    # middleware already skips the /static prefix.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
