"""Authentication routes (spec 6.4, 6.5)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from src.api.auth import CSRF_COOKIE, SESSION_COOKIE, Session
from src.api.deps import Authed, State
from src.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    """Login request."""

    passphrase: Annotated[str, Field(min_length=1, max_length=512)]


class PassphraseBody(BaseModel):
    """Passphrase change request."""

    current: Annotated[str, Field(min_length=1, max_length=512)]
    new: Annotated[str, Field(min_length=1, max_length=512)]


class SessionOut(BaseModel):
    """Who is signed in."""

    authenticated: bool
    csrf_token: str | None = None
    remote: bool = False
    expires_at: str | None = None
    access_mode: str = "local"


def _secure(request: Request, settings: Settings) -> bool:
    return request.url.scheme == "https" or bool(settings.tls_cert_file)


def _set_cookies(
    response: Response, request: Request, settings: Settings, token: str, session: Session
) -> None:
    max_age = int(settings.session_max_days * 86400)
    secure = _secure(request, settings)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, state: State) -> SessionOut:
    """Sign in with the operator passphrase."""
    created = await state.auth.login(
        body.passphrase,
        remote_addr=_client(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_cookies(response, request, state.settings, created.token, created.session)
    s = created.session
    return SessionOut(
        authenticated=True,
        csrf_token=s.csrf_token,
        remote=s.is_remote,
        expires_at=s.expires_at,
        access_mode=state.settings.access_mode,
    )


@router.post("/logout")
async def logout(response: Response, session: Authed, state: State) -> dict[str, bool]:
    """End this session."""
    await state.auth.logout(session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(response: Response, session: Authed, state: State) -> dict[str, int]:
    """Sign out every session (a lost phone)."""
    count = await state.auth.revoke_all()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"revoked": count}


@router.post("/passphrase")
async def change_passphrase(
    body: PassphraseBody, response: Response, session: Authed, state: State
) -> dict[str, bool]:
    """Change the passphrase; every session (including this one) is signed out."""
    await state.auth.set_passphrase(body.current, body.new)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
async def get_session(request: Request, state: State) -> SessionOut:
    """Report whether the cookie holds a live session (never an error, so the UI can branch)."""
    session = await state.auth.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return SessionOut(authenticated=False, access_mode=state.settings.access_mode)
    return SessionOut(
        authenticated=True,
        csrf_token=session.csrf_token,
        remote=session.is_remote,
        expires_at=session.expires_at,
        access_mode=state.settings.access_mode,
    )


@router.get("/sessions")
async def list_sessions(session: Authed, state: State) -> list[dict[str, object]]:
    """Signed-in devices, for the Settings page."""
    return await state.auth.active_sessions()
