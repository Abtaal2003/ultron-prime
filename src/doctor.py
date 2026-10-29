"""``ultron doctor``: check a setup and say in plain words what is wrong and how to fix it.

Never prints a key. ``--live`` also sends one tiny request to each model route Ultron would
actually use, so quota and model-name problems show up before the first chat.
"""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.config import PROJECT_ROOT, Settings, parse_route

Level = Literal["ok", "warn", "fail"]
ROLES = ("main", "guard", "memory_judge", "quarantine_reader", "quarantine_reader_vision")


@dataclass(slots=True)
class Finding:
    """One line of the report."""

    level: Level
    topic: str
    message: str


def effective_route(settings: Settings, role: str) -> str | None:
    """The first route for ``role`` whose provider has a key: what will actually answer."""
    keys = settings.key_presence()
    for raw in [getattr(settings, f"route_{role}"), *settings.fallback_chain]:
        route = parse_route(raw)
        if keys.get(route.provider):
            return str(route)
    return None


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError:
        return False
    return True


def check(settings: Settings) -> list[Finding]:
    """Everything that can be checked without calling a model."""
    out: list[Finding] = []
    if not settings.charter_file.is_file():
        out.append(Finding("fail", "charter", f"missing at {settings.charter_file}"))
    keys = settings.key_presence()
    named = [name for name, present in keys.items() if present and name != "tavily"]
    if named:
        out.append(Finding("ok", "keys", "model keys present: " + ", ".join(named)))
    else:
        out.append(
            Finding(
                "fail",
                "keys",
                "no model key set. Put at least one of ANTHROPIC_API_KEY, GROQ_API_KEY or "
                "GEMINI_API_KEY in .env (Groq and Gemini have free tiers).",
            )
        )
    for role in ROLES:
        route = effective_route(settings, role)
        if route is None:
            if named:
                out.append(
                    Finding(
                        "fail",
                        f"route {role}",
                        f"no configured route for {role} has a key. Set ROUTE_{role.upper()} "
                        "or FALLBACK_CHAIN to a provider you have a key for.",
                    )
                )
            continue
        wanted = getattr(settings, f"route_{role}")
        note = "" if route == wanted else f" (ROUTE_{role.upper()} has no key; falling back)"
        out.append(Finding("ok", f"route {role}", route + note))
    main = effective_route(settings, "main") or ""
    if main.startswith("groq:") and not settings.max_request_tokens:
        out.append(
            Finding(
                "warn",
                "free tier",
                "Groq's free tier rejects requests over 8,000 tokens. Set MAX_REQUEST_TOKENS=8000 "
                "and LLM_MAX_WAIT_S=60 (see .env.example, 'Groq free tier').",
            )
        )
    for label, path in (("data folder", settings.data_dir), ("workspace", settings.workspace_dir)):
        out.append(
            Finding("ok", label, str(path))
            if _writable(path)
            else Finding("fail", label, f"{path} is not writable")
        )
    if not settings.container:
        index = PROJECT_ROOT / "web" / "dist" / "index.html"
        out.append(
            Finding("ok", "web app", "built")
            if index.is_file()
            else Finding(
                "warn",
                "web app",
                "not built, so only the API will run. Build it with: cd web && npm ci && "
                "npm run build (or use Docker, which includes it).",
            )
        )
    if settings.embedding_backend.startswith("local"):
        if importlib.util.find_spec("sentence_transformers") is None:
            out.append(
                Finding(
                    "fail",
                    "embeddings",
                    f"EMBEDDING_BACKEND={settings.embedding_backend} needs the embeddings extra: "
                    "uv sync --extra embeddings. Or set EMBEDDING_BACKEND=gemini (needs a "
                    "Gemini key) or hashing (offline, much weaker recall).",
                )
            )
        else:
            out.append(
                Finding(
                    "ok",
                    "embeddings",
                    f"{settings.embedding_backend} (the model downloads on first use)",
                )
            )
    elif settings.embedding_backend == "hashing":
        out.append(
            Finding(
                "warn",
                "embeddings",
                "hashing is for tests and offline use; memory and past-chat recall work much "
                "better with local or gemini.",
            )
        )
    out.append(_sandbox_finding(settings))
    return out


def _sandbox_finding(settings: Settings) -> Finding:
    from src.tools.sandbox import network_isolation_prefix

    if settings.sandbox_network == "host":
        where = "inside the container" if settings.container else "on this machine"
        return Finding(
            "warn" if not settings.container else "ok",
            "sandbox",
            f"runs code {where} with network access (SANDBOX_NETWORK=host); "
            "runs in turns that read outside content need your approval.",
        )
    if network_isolation_prefix() is None:
        return Finding(
            "warn",
            "sandbox",
            "this system can't cut code off from the network (no unprivileged user "
            "namespaces), so the sandbox will refuse to run. Use Docker, or set "
            "SANDBOX_NETWORK=host if you are the only user.",
        )
    return Finding("ok", "sandbox", "code runs with no network access")


async def live(settings: Settings) -> list[Finding]:
    """Send one tiny request through each distinct route Ultron would use."""
    from src.core.llm.base import ChatRequest, Message
    from src.core.wiring import build_providers

    providers = build_providers(settings)
    out: list[Finding] = []
    seen: set[str] = set()
    for role in ROLES:
        route = effective_route(settings, role)
        if route is None or route in seen:
            continue
        seen.add(route)
        spec = parse_route(route)
        provider = providers.get(spec.provider)
        if provider is None:
            continue
        request = ChatRequest(
            model=spec.model,
            messages=[Message.user("Reply with the word ok.")],
            max_tokens=256,
            allow_thinking=False,
            role="doctor",
        )
        try:
            await asyncio.wait_for(provider.chat(request), timeout=45)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            out.append(Finding("fail", f"live {route}", _explain(str(detail))))
        else:
            out.append(Finding("ok", f"live {route}", "answered"))
    return out


def _explain(detail: str) -> str:
    low = detail.lower()
    if "per day" in low or "(tpd)" in low or "(rpd)" in low or "quota" in low:
        return "the free daily quota is used up; it refills over the next 24 hours."
    if "401" in low or "invalid api key" in low or "unauthorized" in low or "permission" in low:
        return "the key was refused; check it in .env (no quotes, no spaces)."
    if "not found" in low or "does not exist" in low or "404" in low:
        return "the provider doesn't know this model name; change the route in .env."
    return "failed: " + detail[:160]


def report(findings: list[Finding], write: Callable[[str], object]) -> int:
    """Print the findings and return the exit code (1 if anything failed)."""
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    for f in findings:
        write(f"[{marks[f.level]}] {f.topic}: {f.message}\n")
    failed = sum(f.level == "fail" for f in findings)
    warned = sum(f.level == "warn" for f in findings)
    write(f"\n{failed} problem(s), {warned} warning(s).\n")
    return 1 if failed else 0
