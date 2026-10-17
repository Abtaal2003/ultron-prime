"""Operator authentication: Argon2 passphrase, server-side sessions, CSRF, login lockout (spec 6.5).

Only the Argon2 hash of the passphrase and the SHA-256 of each session token are stored, so a
copy of the database never lets anyone sign in. The session cookie is HttpOnly and SameSite=Strict;
state-changing requests also need a CSRF token (double submit, checked against the session too).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from src.config import Settings
from src.core.db import Database
from src.core.errors import PermissionFailure, RateLimited, ValidationFailure

SESSION_COOKIE = "ultron_session"
CSRF_COOKIE = "ultron_csrf"
CSRF_HEADER = "x-csrf-token"
MIN_PASSPHRASE_CHARS = 12
LOCKOUT_CAP_S = 3600.0
LOCKOUT_BASE_S = 30.0
_TOUCH_EVERY_S = 60.0
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hash_token(token: str) -> str:
    """The stored form of a session token."""
    return hashlib.sha256(token.encode()).hexdigest()


def is_loopback(addr: str | None) -> bool:
    """Whether a client address is local to this machine."""
    return (addr or "") in LOOPBACK


@dataclass(slots=True, frozen=True)
class Session:
    """A validated operator session."""

    id: str  # the stored hash, never the cookie value
    csrf_token: str
    created_at: str
    expires_at: str
    is_remote: bool
    remote_addr: str | None
    user_agent: str | None


@dataclass(slots=True, frozen=True)
class NewSession:
    """What login hands back to the route (the raw token exists only here)."""

    token: str
    session: Session


class AuthService:
    """Passphrase check, session lifecycle, and brute-force lockout."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._hasher = PasswordHasher()
        self._touched: dict[str, float] = {}

    # ------------------------------------------------------------------ passphrase --
    async def ensure_passphrase(self) -> str | None:
        """On first run create an operator passphrase and return it once (else ``None``)."""
        if await self.db.scalar("SELECT COUNT(*) FROM operator_auth"):
            return None
        passphrase = secrets.token_urlsafe(18)
        await self._store_hash(passphrase)
        await self.db.audit("auth.passphrase_created", "system")
        return passphrase

    async def _store_hash(self, passphrase: str) -> None:
        digest = self._hasher.hash(passphrase)
        await self.db.execute(
            "INSERT INTO operator_auth(id, passphrase_hash, updated_at) VALUES (1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET passphrase_hash=excluded.passphrase_hash, "
            "updated_at=excluded.updated_at",
            (digest, _iso(datetime.now(UTC))),
        )

    async def set_passphrase(self, current: str, new: str) -> None:
        """Change the passphrase (needs the current one) and sign out every session."""
        if len(new) < MIN_PASSPHRASE_CHARS:
            raise ValidationFailure(f"use at least {MIN_PASSPHRASE_CHARS} characters")
        if not await self._verify(current):
            raise PermissionFailure("current passphrase is wrong")
        await self._store_hash(new)
        await self.revoke_all()
        await self.db.audit("auth.passphrase_changed", "operator")

    async def _verify(self, passphrase: str) -> bool:
        row = await self.db.fetchone("SELECT passphrase_hash FROM operator_auth WHERE id=1")
        if row is None:
            return False
        try:
            return bool(self._hasher.verify(row["passphrase_hash"], passphrase))
        except (VerificationError, InvalidHashError):
            return False

    # ---------------------------------------------------------------------- login --
    async def login(
        self, passphrase: str, *, remote_addr: str | None, user_agent: str | None
    ) -> NewSession:
        """Verify the passphrase and open a session. Failures lock the address out, escalating."""
        key = remote_addr or "unknown"
        await self._check_lockout(key)
        if not await self._verify(passphrase):
            await self._record_failure(key)
            await self.db.audit("auth.login_failed", "unknown", {"addr": key})
            raise PermissionFailure("wrong passphrase")
        await self.db.execute("DELETE FROM login_attempts WHERE key=?", (key,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = datetime.now(UTC)
        expires = now + timedelta(days=self.settings.session_max_days)
        remote = not is_loopback(remote_addr)
        sid = hash_token(token)
        await self.db.execute(
            "INSERT INTO sessions(id, csrf_token, created_at, last_seen_at, expires_at, "
            "user_agent, remote_addr, is_remote) VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                csrf,
                _iso(now),
                _iso(now),
                _iso(expires),
                (user_agent or "")[:200],
                key,
                int(remote),
            ),
        )
        await self.db.audit("auth.login", "operator", {"remote": remote})
        session = Session(sid, csrf, _iso(now), _iso(expires), remote, key, user_agent)
        return NewSession(token, session)

    async def _check_lockout(self, key: str) -> None:
        row = await self.db.fetchone("SELECT locked_until FROM login_attempts WHERE key=?", (key,))
        if row and row["locked_until"] > time.time():
            wait = int(row["locked_until"] - time.time()) + 1
            raise RateLimited(f"too many attempts; try again in {wait} s")

    async def _record_failure(self, key: str) -> None:
        row = await self.db.fetchone("SELECT failures FROM login_attempts WHERE key=?", (key,))
        failures = (row["failures"] if row else 0) + 1
        over = failures - self.settings.login_max_attempts
        locked = time.time() + min(LOCKOUT_BASE_S * 2**over, LOCKOUT_CAP_S) if over >= 0 else 0.0
        await self.db.execute(
            "INSERT INTO login_attempts(key, failures, locked_until, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, "
            "locked_until=excluded.locked_until, updated_at=excluded.updated_at",
            (key, failures, locked, _iso(datetime.now(UTC))),
        )

    # -------------------------------------------------------------------- sessions --
    async def get_session(self, token: str | None) -> Session | None:
        """Return the live session for a cookie value, enforcing idle and absolute expiry."""
        if not token:
            return None
        sid = hash_token(token)
        row = await self.db.fetchone("SELECT * FROM sessions WHERE id=? AND revoked=0", (sid,))
        if row is None:
            return None
        now = datetime.now(UTC)
        idle = timedelta(hours=self.settings.session_idle_hours)
        if now >= _parse(row["expires_at"]) or now - _parse(row["last_seen_at"]) >= idle:
            await self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (sid,))
            return None
        if time.monotonic() - self._touched.get(sid, 0.0) > _TOUCH_EVERY_S:
            self._touched[sid] = time.monotonic()
            await self.db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_iso(now), sid))
        return Session(
            sid,
            row["csrf_token"],
            row["created_at"],
            row["expires_at"],
            bool(row["is_remote"]),
            row["remote_addr"],
            row["user_agent"],
        )

    async def logout(self, session: Session) -> None:
        """End one session."""
        await self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (session.id,))

    async def revoke_all(self) -> int:
        """Sign out every session (a lost phone)."""
        count = int(await self.db.scalar("SELECT COUNT(*) FROM sessions WHERE revoked=0") or 0)
        await self.db.execute("UPDATE sessions SET revoked=1 WHERE revoked=0")
        await self.db.audit("auth.revoke_all", "operator", {"count": count})
        return count

    async def active_sessions(self) -> list[dict[str, object]]:
        """Live sessions, for the Settings page (ids are never returned)."""
        rows = await self.db.fetchall(
            "SELECT created_at, last_seen_at, user_agent, remote_addr, is_remote FROM sessions "
            "WHERE revoked=0 AND expires_at > ? ORDER BY last_seen_at DESC",
            (_iso(datetime.now(UTC)),),
        )
        return [dict(r) for r in rows]


def csrf_matches(session: Session, cookie: str | None, header: str | None) -> bool:
    """Double-submit check, also bound to the session's own token."""
    if not cookie or not header:
        return False
    return hmac.compare_digest(cookie, header) and hmac.compare_digest(session.csrf_token, header)
