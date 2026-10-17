"""FastAPI application factory (spec 6.4, 6.5).

One ASGI process serves the JSON API, the SSE streams, and (when built) the React frontend.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.api.auth import AuthService
from src.api.deps import AppState, Unauthenticated, error_response
from src.api.middleware import BodyLimit, OriginCheck, SecurityHeaders
from src.api.routes import auth, chats, files, memory, system, turns
from src.config import PROJECT_ROOT, Settings, get_settings
from src.core.errors import UltronError
from src.core.wiring import Runtime, build_runtime
from src.logging_setup import get_logger

log = get_logger("api")
API_PREFIX = "/api/v1"
WEB_DIST = PROJECT_ROOT / "web" / "dist"


def _print_first_run(passphrase: str) -> None:
    bar = "=" * 64
    sys.stderr.write(
        f"\n{bar}\n  ULTRON PRIME: operator passphrase (shown once, store it safely)\n\n"
        f"    {passphrase}\n\n  Change it later in Settings.\n{bar}\n\n"
    )
    sys.stderr.flush()


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    """Build the application. Pass ``runtime`` in tests to inject fakes."""
    cfg = runtime.settings if runtime else (settings or get_settings())
    dist = web_dist if web_dist is not None else WEB_DIST

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime or await build_runtime(cfg)
        auth_service = AuthService(rt.db, cfg)
        first = await auth_service.ensure_passphrase()
        if first:
            _print_first_run(first)
        await system.load_overrides(rt.db, cfg, rt.agent.disabled_tools)
        await rt.bus.purge_expired(cfg.turn_event_retention_hours)
        await rt.agent.recover_interrupted()
        app.state.ultron = AppState(runtime=rt, auth=auth_service)
        log.info("api_ready", access_mode=cfg.access_mode, host=cfg.host, port=cfg.port)
        try:
            yield
        finally:
            if runtime is None:  # an injected runtime belongs to its creator
                await rt.close()

    app = FastAPI(
        title="Ultron Prime",
        version="1.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json" if cfg.access_mode == "local" else None,
    )

    @app.exception_handler(UltronError)
    async def _ultron_error(_: Request, exc: UltronError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(Unauthenticated)
    async def _unauth(_: Request, __: Unauthenticated) -> JSONResponse:
        return JSONResponse(
            {"error": {"kind": "permission", "message": "sign in required"}}, status_code=401
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        kind = {404: "not_found", 405: "validation", 401: "permission", 403: "permission"}.get(
            exc.status_code, "internal"
        )
        return JSONResponse(
            {"error": {"kind": kind, "message": str(exc.detail)}}, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", error=type(exc).__name__)
        return JSONResponse(
            {"error": {"kind": "internal", "message": "something went wrong"}}, status_code=500
        )

    api = API_PREFIX
    app.include_router(system.public, prefix=api)
    for module in (auth, chats, turns, memory, files, system):
        app.include_router(module.router, prefix=api)

    if dist.is_dir() and (dist / "index.html").is_file():
        _mount_frontend(app, dist)

    # Middleware: the last added runs first (outermost).
    app.add_middleware(BodyLimit, max_bytes=cfg.max_request_mb * 1024 * 1024)
    app.add_middleware(OriginCheck, allowed=list(cfg.allowed_origins))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.allowed_hosts))
    app.add_middleware(SecurityHeaders, hsts=bool(cfg.tls_cert_file))
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse(
                {"error": {"kind": "not_found", "message": "no such route"}}, status_code=404
            )
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
