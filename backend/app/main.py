import logging
import hmac
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deps import CSRF_HEADER, SAFE_METHODS
from app.providers import build_provider
from app.rate_limit import RateLimiter
from app.routers import auth, health, history, imports, leads, search, users

logger = logging.getLogger("app")

API_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="ICP Lead Finder API",
        version=__version__,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    engine = make_engine(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.limiter = RateLimiter()
    app.state.provider = build_provider(
        settings.resolved_contact_provider, apollo_api_key=settings.apollo_api_key,
    )

    if not settings.auth_required:
        logger.warning("AUTH_REQUIRED is false: sign-in is off and every visitor has access to all data")

    trusted_origins = set(settings.cors_origin_list)

    @app.middleware("http")
    async def security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/") and settings.sites_proxy_secret is not None:
            supplied = request.headers.get("x-icp-sites-key", "")
            expected = settings.sites_proxy_secret.get_secret_value()
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse(status_code=403, content={"detail": "Access denied"},
                                    headers={"Cache-Control": "no-store"})
        # Reject state-changing requests sent by browsers from untrusted sites.
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin and origin.rstrip("/") not in trusted_origins:
            logger.warning("Rejected %s %s from untrusted origin %r", request.method, request.url.path, origin[:200])
            return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})

        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            for header, value in API_SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
        else:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    if trusted_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(trusted_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type", CSRF_HEADER],
        )

    for module in (health, auth, users, search, leads, imports, history):
        app.include_router(module.router)

    return app


app = create_app()

