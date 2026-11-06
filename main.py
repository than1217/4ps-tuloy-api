import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis

import database
from config import MAX_BODY_BYTES, Settings, get_settings
from errors import error_body, install_error_handlers
from observability import configure_logging, new_request_id, request_id_var
from ratelimit import FixedWindowLimiter
from utils import cache_key_builder
from routers import auth, beneficiaries, docs_portal, health, staff, system, payments, bcmd, cor_review

logger = logging.getLogger("tuloy.app")

MAX_SETTINGS_BODY_BYTES = 10_000_000


async def _init_cache(settings: Settings):
    if settings.redis_url:
        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        try:
            await client.ping()
            FastAPICache.init(RedisBackend(client), prefix="tuloy-api-cache",
                              expire=settings.cache_expire_seconds, key_builder=cache_key_builder)
            logger.info("cache_ready", extra={"backend": "redis", "ttl_seconds": settings.cache_expire_seconds})
            return client
        except Exception as exc:
            await client.aclose()
            logger.warning("cache_redis_unavailable", extra={"exc_type": type(exc).__name__})
    FastAPICache.init(InMemoryBackend(), prefix="tuloy-api-cache",
                      expire=settings.cache_expire_seconds, key_builder=cache_key_builder)
    logger.info("cache_ready", extra={"backend": "memory", "ttl_seconds": settings.cache_expire_seconds})
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.secret_key_ephemeral:
        logger.warning("secret_key_ephemeral", extra={"note": "local only; tokens stop working on restart"})
    redis_client = await _init_cache(settings)
    # No schema changes happen here. A database or tunnel outage at startup is not fatal:
    # /readyz reports it and requests retry the connection.
    try:
        database.init(settings)
    except Exception as exc:
        logger.error("db_init_failed", extra=database.connect_failure(exc, settings))
    try:
        yield
    finally:
        if redis_client is not None:
            await redis_client.aclose()
        database.close()
        logger.info("shutdown_complete")


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        return route.path
    # Plain Starlette routes set only "endpoint"; they take no path params.
    if request.scope.get("endpoint") is not None and not request.scope.get("path_params"):
        return request.url.path
    return "<unmatched>"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    bcmd.validate_local_candidate(settings)
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Tuloy API",
        version="0.2.0",
        lifespan=lifespan,
        # No /docs, /redoc or /openapi.json: the docs portals (/tuloy/docs, /tuloydswd/docs) serve
        # trimmed specs instead.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bcmd_settings = settings
    install_error_handlers(app)
    limiter = FixedWindowLimiter(settings.rate_limit_per_minute, 60)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = new_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status_code = 500
        try:
            length = request.headers.get("content-length")
            body_limit = MAX_SETTINGS_BODY_BYTES if request.url.path == "/PostAppSettings" else MAX_BODY_BYTES
            if length and length.isdigit() and int(length) > body_limit:
                status_code = 413
                return JSONResponse(error_body(413, "Request body too large"), status_code=413,
                                    headers={"X-Request-ID": rid})
            client_ip = request.client.host if request.client else "unknown"
            if request.url.path not in ("/healthz", "/readyz"):
                allowed, retry_after = limiter.hit(client_ip)
                if not allowed:
                    status_code = 429
                    return JSONResponse(error_body(429, "Too many requests"), status_code=429,
                                        headers={"Retry-After": str(retry_after), "X-Request-ID": rid})
            try:
                response = await call_next(request)
            except Exception:
                # Unhandled error: answer with the standard body and request ID; the log line
                # carries the exception type and frames only, never its message.
                logger.exception("unhandled_error")
                response = JSONResponse(error_body(500, "Internal server error", "INTERNAL_ERROR"),
                                        status_code=500)
            status_code = response.status_code
            response.headers["X-Request-ID"] = rid
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            # Docs pages set their own (looser) policy; everything else gets the strict one.
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            return response
        finally:
            logger.info("request", extra={
                "method": request.method,
                # Route template, not the concrete path: path parameters can carry identifiers.
                "path": _route_template(request),
                "status": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            })
            request_id_var.reset(token)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(beneficiaries.router)
    app.include_router(staff.router)
    app.include_router(system.router)
    app.include_router(payments.router)
    app.include_router(bcmd.router)
    app.include_router(cor_review.router)
    if settings.partner_api_enabled:
        from partner.router import router as partner_router
        app.include_router(partner_router)
    if settings.expose_docs:
        docs_portal.mount(app)
    return app


app = create_app()
