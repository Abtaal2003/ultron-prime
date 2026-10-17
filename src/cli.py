"""The ``ultron`` command line (spec 14, 17)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, Settings, get_settings


def _bind_host(settings: Settings) -> str:
    """Resolve the interface to bind for the access mode (spec 6.5)."""
    if settings.access_mode == "local":
        return "127.0.0.1"
    if settings.host in {"127.0.0.1", "localhost", ""}:
        sys.stderr.write(
            f"ACCESS_MODE={settings.access_mode} needs HOST set to the {settings.access_mode} "
            "interface address (for example your Tailscale IP). Refusing to guess.\n"
        )
        raise SystemExit(2)
    if settings.host in {"0.0.0.0", "::"}:  # noqa: S104 - rejected on purpose
        sys.stderr.write("Binding to all interfaces is not supported; name one interface.\n")
        raise SystemExit(2)
    return settings.host


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web server."""
    import uvicorn

    from src.api.app import create_app

    settings = get_settings()
    host = _bind_host(settings)
    port = args.port or settings.port
    kwargs: dict[str, object] = {}
    if settings.tls_cert_file and settings.tls_key_file:
        kwargs.update(ssl_certfile=settings.tls_cert_file, ssl_keyfile=settings.tls_key_file)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
        server_header=False,
        **kwargs,  # type: ignore[arg-type]
    )
    return 0


def cmd_export_schemas(args: argparse.Namespace) -> int:
    """Write the OpenAPI and event schemas for the frontend generator."""
    from src.api.export import export_schemas

    for path in export_schemas(Path(args.out)):
        sys.stdout.write(f"wrote {path}\n")
    return 0


def cmd_reset_passphrase(_: argparse.Namespace) -> int:
    """Generate a new operator passphrase and sign out every session."""
    from src.api.auth import AuthService
    from src.core.db import Database

    async def run() -> str:
        settings = get_settings()
        db = Database(settings.sqlite_file)
        await db.connect()
        try:
            auth = AuthService(db, settings)
            await db.execute("DELETE FROM operator_auth")
            await auth.revoke_all()
            passphrase = await auth.ensure_passphrase()
            assert passphrase is not None
            return passphrase
        finally:
            await db.close()

    sys.stdout.write(f"New operator passphrase (shown once): {asyncio.run(run())}\n")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    """Validate configuration and model routing without starting the server."""
    settings = get_settings()
    problems: list[str] = []
    if not settings.charter_file.is_file():
        problems.append(f"charter missing at {settings.charter_file}")
    for provider, present in settings.key_presence().items():
        sys.stdout.write(f"{provider:10s} key {'present' if present else 'absent'}\n")
    if not any(settings.key_presence().values()):
        problems.append("no provider key set; the agent cannot answer")
    for line in problems:
        sys.stdout.write(f"PROBLEM: {line}\n")
    sys.stdout.write(f"project root: {PROJECT_ROOT}\n")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="ultron", description="Ultron Prime")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the web server (default)")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=cmd_serve)
    export = sub.add_parser("export-schemas", help="write OpenAPI and event schemas")
    export.add_argument("--out", default=str(PROJECT_ROOT / "web" / "src" / "lib" / "schemas"))
    export.set_defaults(func=cmd_export_schemas)
    sub.add_parser("reset-passphrase", help="issue a new operator passphrase").set_defaults(
        func=cmd_reset_passphrase
    )
    sub.add_parser("check", help="validate configuration").set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
