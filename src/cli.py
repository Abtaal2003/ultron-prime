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
        # Inside the Docker image "local" means the published port, which compose binds to
        # the host's loopback only; the container's own loopback would be unreachable.
        return "0.0.0.0" if settings.container else "127.0.0.1"  # noqa: S104
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check keys, routes, folders, embeddings and the sandbox; ``--live`` pings each route."""
    from src import doctor

    settings = get_settings()
    findings = doctor.check(settings)
    if getattr(args, "live", False):
        findings += asyncio.run(doctor.live(settings))
    code = doctor.report(findings, sys.stdout.write)
    sys.stdout.write(f"project root: {PROJECT_ROOT}\n")
    return code


cmd_check = cmd_doctor


def _setup_logging() -> None:
    """Configure logging before anything can fail (secrets hidden, no locals in tracebacks)."""
    from src.logging_setup import configure_from_settings, configure_logging

    try:
        configure_from_settings(get_settings())
    except Exception:  # a bad config must still get safe logging
        configure_logging()


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
    for name in ("doctor", "check"):
        doc = sub.add_parser(name, help="check the setup and explain any problem")
        doc.add_argument("--live", action="store_true", help="also send one tiny request per route")
        doc.set_defaults(func=cmd_check if name == "check" else cmd_doctor)
    args = parser.parse_args(argv)
    _setup_logging()
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"])
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
