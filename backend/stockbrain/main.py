"""FastAPI application factory and process entrypoint.

One process owns everything: the HTTP API, and (from later phases) the Alpaca
WebSocket task, the Telegram long-polling application, the scheduler and the
PostgreSQL-backed job workers.  They are started and stopped by the lifespan
handler so a failure in an optional subsystem degrades that subsystem instead of
killing the process.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from stockbrain.api.auth import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    STATE_CHANGING_METHODS,
    is_public_path,
    read_session,
    verify_origin,
    web_auth_blockers,
)
from stockbrain.api.routes import auth as auth_routes
from stockbrain.api.routes import discovery as discovery_routes
from stockbrain.api.routes import events as events_routes
from stockbrain.api.routes import execution as execution_routes
from stockbrain.api.routes import health as health_routes
from stockbrain.api.routes import instruments as instrument_routes
from stockbrain.api.routes import proposals as proposal_routes
from stockbrain.api.routes import research as research_routes
from stockbrain.api.routes import system as system_routes
from stockbrain.config import Settings, get_settings
from stockbrain.db.base import utcnow as _utcnow
from stockbrain.db.session import Database
from stockbrain.errors import (
    ProposalAlreadyConsumed,
    ProposalExpired,
    ProposalInvalidated,
    RiskBlocked,
    StockBrainError,
)
from stockbrain.logging import configure_logging, get_logger
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.observability.metrics import METRICS
from stockbrain.services import ServiceContainer
from stockbrain.startup import (
    check_database,
    check_schema_current,
    instance_identity,
    register_static_provider_states,
)

__all__ = ["create_app"]

log = get_logger(__name__)

#: Populated by the Docker build; absent during local backend-only development.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    registry: ProviderHealthRegistry = app.state.health_registry
    database: Database = app.state.database

    log.info(
        "startup_begin",
        app_env=settings.app_env.value,
        instance=instance_identity(),
        broker_environment=settings.t212_env.value,
        live_execution_permitted=settings.live_execution_permitted,
    )

    register_static_provider_states(settings, registry)

    database_ok = await check_database(database, registry)
    if database_ok:
        schema_current, schema_detail = await check_schema_current(database)
        registry.set_schema_state(schema_current, schema_detail)
        if not schema_current:
            # Not fatal: the API still answers /health so an operator can see
            # exactly what is wrong instead of a crash-looping container.
            log.error("schema_not_current", detail=schema_detail)
        await registry.persist(database)
    else:
        registry.set_schema_state(False, "database unavailable")

    if not settings.live_execution_permitted:
        log.warning(
            "live_execution_disabled",
            blockers=settings.execution_blockers,
        )

    # Said loudly on every start, because an unauthenticated API in front of a
    # broker is the one configuration mistake nothing downstream can catch.
    if auth_blockers := web_auth_blockers(settings):
        log.warning("web_authentication_not_protecting", blockers=auth_blockers)

    services: ServiceContainer | None = None
    if database_ok:
        # Discovery, the job workers and the scheduler all need the database.
        # Without it they cannot start, but the API still serves /api/health so
        # an operator can see precisely why.
        services = ServiceContainer(settings=settings, database=database, health=registry)
        try:
            await services.start(instance_id=instance_identity())
        except Exception as exc:
            log.exception("discovery_subsystem_start_failed", error=str(exc))
            await services.stop()
            services = None
    app.state.services = services

    log.info(
        "startup_complete",
        database_ok=database_ok,
        discovery_started=services is not None,
    )
    try:
        yield
    finally:
        log.info("shutdown_begin")
        if services is not None:
            await services.stop()
        await database.dispose()
        log.info("shutdown_complete")


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _register_auth_middleware(app, settings)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind a request id to the logging context and record basic metrics."""
        structlog.contextvars.clear_contextvars()
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            METRICS.inc("stockbrain_http_requests_total", labels={"status": "500"})
            log.exception("request_failed", path=request.url.path, method=request.method)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        METRICS.inc("stockbrain_http_requests_total", labels={"status": str(response.status_code)})
        response.headers["x-request-id"] = request_id
        if request.url.path.startswith("/api"):
            log.debug(
                "request_completed",
                path=request.url.path,
                method=request.method,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        return response


def _register_auth_middleware(app: FastAPI, settings: Settings) -> None:
    """Deny by default; allow only what :func:`is_public_path` names.

    A middleware rather than a per-route dependency, because a dependency has to
    be *remembered* on every new route and this has to be *un*-remembered. The
    failure mode of forgetting is a 401 instead of an open door, and
    ``tests/unit/test_web_auth.py`` enumerates the route table to prove no path
    escaped.

    Registered before the request-context middleware so it runs *after* it --
    Starlette applies middleware in reverse registration order -- which means a
    rejected request still gets a request id and still appears in the metrics.
    """

    @app.middleware("http")
    async def require_session(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not settings.web_auth_enabled or is_public_path(path):
            return await call_next(request)

        blockers = web_auth_blockers(settings)
        if blockers:
            # Authentication is on and unusable: no password hash, or no signing
            # key. Refusing everything is the only safe reading -- "cannot check
            # a credential" must never mean "do not check a credential".
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "web authentication is enabled but not configured",
                    "blockers": blockers,
                },
            )

        session = read_session(
            request.cookies.get(SESSION_COOKIE_NAME),
            secret=settings.stockbrain_secret_key.get_secret_value(),
            now=_utcnow(),
        )
        if session is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required"},
                headers={"www-authenticate": "Cookie"},
            )

        if request.method.upper() in STATE_CHANGING_METHODS:
            # Three independent checks, each of which fails closed, because what
            # is behind these routes is an irreversible broker order.
            if not verify_origin(
                request.headers.get("origin"),
                request.headers.get("referer"),
                request.headers.get("host"),
            ):
                log.warning("csrf_origin_rejected", path=path, method=request.method)
                return JSONResponse(
                    status_code=403, content={"detail": "cross-origin request refused"}
                )
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
            header_token = request.headers.get(CSRF_HEADER_NAME)
            if (
                not cookie_token
                or not header_token
                or not compare_digest(cookie_token, header_token)
            ):
                log.warning("csrf_token_rejected", path=path, method=request.method)
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            f"a valid {CSRF_HEADER_NAME} header matching the "
                            f"{CSRF_COOKIE_NAME} cookie is required"
                        )
                    },
                )

        request.state.session = session
        return await call_next(request)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProposalAlreadyConsumed)
    async def _already_consumed(_: Request, exc: ProposalAlreadyConsumed) -> JSONResponse:
        # 409 is the contract for "the other client won the race" (spec 33).
        return JSONResponse(status_code=409, content={"detail": str(exc) or "conflict"})

    @app.exception_handler(ProposalExpired)
    async def _expired(_: Request, exc: ProposalExpired) -> JSONResponse:
        return JSONResponse(status_code=410, content={"detail": str(exc) or "proposal expired"})

    @app.exception_handler(ProposalInvalidated)
    async def _invalidated(_: Request, exc: ProposalInvalidated) -> JSONResponse:
        # 409 for the same reason as ProposalAlreadyConsumed: the client is
        # acting on a proposal whose world moved on.
        return JSONResponse(status_code=409, content={"detail": str(exc) or "proposal invalidated"})

    @app.exception_handler(RiskBlocked)
    async def _risk_blocked(_: Request, exc: RiskBlocked) -> JSONResponse:
        # 422: the request was well formed and the entity is real; the
        # deterministic risk engine simply refused, and that refusal is final.
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "blocked_rules": list(exc.rule_ids)},
        )

    @app.exception_handler(StockBrainError)
    async def _domain_error(_: Request, exc: StockBrainError) -> JSONResponse:
        log.warning("domain_error", error_type=type(exc).__name__, error=str(exc))
        return JSONResponse(status_code=400, content={"detail": str(exc)})


def _mount_frontend(app: FastAPI) -> None:
    """Serve the compiled SPA from the same origin as the API.

    Keeping the frontend same-origin means session cookies can stay
    ``SameSite=Strict`` and no separate web server is needed in the container.
    """
    if not FRONTEND_DIST.is_dir():
        log.info("frontend_bundle_absent", path=str(FRONTEND_DIST))
        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        # Unmatched API paths must 404 rather than silently returning the SPA
        # shell, which would turn a typo into a confusing 200.
        if full_path.startswith(("api/", "metrics")):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        # Any other unmatched path is handed to the client-side router.
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Event-driven equity research and trade-proposal system. "
            "Every broker order requires explicit human approval."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    app.state.settings = settings
    app.state.database = Database(settings)
    app.state.health_registry = ProviderHealthRegistry()

    _register_middleware(app, settings)
    _register_exception_handlers(app)

    app.state.services = None

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    app.include_router(events_routes.router)
    app.include_router(discovery_routes.router)
    app.include_router(instrument_routes.router)
    app.include_router(research_routes.router)
    app.include_router(proposal_routes.router)
    app.include_router(execution_routes.router)

    _mount_frontend(app)
    return app


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the server.

    The application is constructed here rather than at module import so that
    importing :mod:`stockbrain.main` has no side effects -- tests build their
    own app with their own settings.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
