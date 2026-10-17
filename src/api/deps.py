"""Request-scoped dependencies: runtime access, authentication, CSRF, error mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from src.api.auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthService,
    Session,
    csrf_matches,
)
from src.config import Settings
from src.core.errors import ErrorKind, PermissionFailure, UltronError
from src.core.wiring import Runtime

STATUS_FOR: dict[ErrorKind, int] = {
    "validation": 422,
    "policy": 403,
    "timeout": 504,
    "not_found": 404,
    "permission": 403,
    "rate_limit": 429,
    "unavailable": 503,
    "limit": 413,
    "conflict": 409,
    "internal": 500,
}


@dataclass(slots=True)
class AppState:
    """Everything routes need, attached to ``app.state.ultron``."""

    runtime: Runtime
    auth: AuthService

    @property
    def settings(self) -> Settings:
        """Live settings."""
        return self.runtime.settings


class Unauthenticated(Exception):
    """No valid session."""


def get_state(request: Request) -> AppState:
    """The application state."""
    state: AppState = request.app.state.ultron
    return state


State = Annotated[AppState, Depends(get_state)]


async def current_session(request: Request, state: State) -> Session:
    """Require a live session; for state-changing methods also require a valid CSRF token."""
    session = await state.auth.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise Unauthenticated
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not csrf_matches(
        session, request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)
    ):
        raise PermissionFailure("missing or invalid CSRF token")
    return session


Authed = Annotated[Session, Depends(current_session)]


def error_response(exc: UltronError) -> JSONResponse:
    """Map a deliberate error onto the wire format."""
    headers = {"Retry-After": "30"} if exc.kind == "rate_limit" else None
    return JSONResponse(
        {"error": {"kind": exc.kind, "message": exc.message}},
        status_code=STATUS_FOR.get(exc.kind, 500),
        headers=headers,
    )
