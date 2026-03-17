from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
    create_session_token,
    is_auth_enabled,
    load_sessions,
    persist_new_session,
    verify_pin,
)
from app.core.config import load_config
from app.core.settings import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
SESSION_COOKIE = "youos_session"
SESSION_MAX_AGE = 86400  # 24 hours


class PinAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login when PIN is configured."""

    SKIP_PREFIXES = ("/login", "/static")

    def __init__(self, app, config: dict):
        super().__init__(app)
        self.config = config
        # Load persisted sessions, prune expired
        persisted = load_sessions()
        self.sessions: set[str] = set(persisted.keys())
        self.limiter = LoginRateLimiter()

    async def dispatch(self, request: Request, call_next):
        if not is_auth_enabled(self.config):
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if token and token in self.sessions:
            return await call_next(request)

        return RedirectResponse(url="/login", status_code=303)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # Clear embedding cache on shutdown
    from app.core.embeddings import clear_embedding_cache

    clear_embedding_cache()


def create_app() -> FastAPI:
    settings = get_settings()
    config = load_config()

    app = FastAPI(
        title="YouOS",
        version="0.1.0",
        description="Your personal AI email copilot — learns your style from your Gmail history.",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.config = config

    auth_middleware = PinAuthMiddleware(app, config)
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_middleware.dispatch)
    app.state.auth = auth_middleware

    # ── Login routes ──
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not is_auth_enabled(config):
            return RedirectResponse(url="/feedback", status_code=303)
        template = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("{{ error }}", ""))

    @app.post("/login")
    async def login_submit(request: Request):
        if not is_auth_enabled(config):
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
        stored_hash = config.get("server", {}).get("pin", "")

        if verify_pin(str(pin), stored_hash):
            auth_middleware.limiter.reset(client_ip)
            token = create_session_token()
            auth_middleware.sessions.add(token)
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
    return app


app = create_app()
