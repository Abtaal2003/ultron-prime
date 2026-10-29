"""Health, status, settings, and red-team report routes (spec 6.4)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, TypeAdapter, ValidationError

from src.api.deps import Authed, State
from src.config import Settings, parse_route
from src.core.db import Database, from_json
from src.core.errors import ValidationFailure
from src.core.events import utcnow_iso
from src.tools.telemetry import TelemetryInput, collect

public = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])

#: Settings the operator may change at runtime. Everything else needs a restart and the .env file.
MUTABLE: tuple[str, ...] = (
    "stream_mode",
    "persona_profanity",
    "persona_quote_callbacks",
    "passive_cross_chat_recall",
    "quarantine_mode",
    "scope_strictness",
    "egress_policy",
    "output_guard_llm_mode",
    "repeat_detection",
    "repeat_cross_chat",
)
_EXPOSED = (
    *MUTABLE,
    "access_mode",
    "effort_tech",
    "effort_review",
    "effort_conversation",
    "effort_refusal",
    "max_tool_iterations",
    "upload_max_mb",
    "session_idle_hours",
    "session_max_days",
    "remote_confirm_tools",
    "enabled_tools",
)


class SettingsPatch(BaseModel):
    """Runtime-changeable settings; only the fields sent are applied."""

    stream_mode: str | None = None
    persona_profanity: str | None = None
    persona_quote_callbacks: bool | None = None
    passive_cross_chat_recall: bool | None = None
    quarantine_mode: str | None = None
    scope_strictness: str | None = None
    egress_policy: str | None = None
    output_guard_llm_mode: str | None = None
    repeat_detection: bool | None = None
    repeat_cross_chat: bool | None = None
    disabled_tools: list[str] | None = None


@public.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Unauthenticated, returns no data."""
    return {"status": "ok"}


async def load_overrides(db: Database, settings: Settings, disabled: set[str]) -> None:
    """Apply persisted operator overrides at startup."""
    for row in await db.fetchall(
        "SELECT key, value_json FROM settings_overrides WHERE key LIKE 'setting:%' "
        "OR key='disabled_tools'"
    ):
        value = from_json(row["value_json"])
        if row["key"] == "disabled_tools":
            disabled.clear()
            disabled.update(str(v) for v in value)
            continue
        name = row["key"].split(":", 1)[1]
        if name in MUTABLE:
            try:
                object.__setattr__(settings, name, _validate(name, value))
            except ValidationFailure:
                continue


def _validate(name: str, value: Any) -> Any:
    field = Settings.model_fields[name]
    try:
        return TypeAdapter(field.annotation or str).validate_python(value)
    except ValidationError as exc:
        raise ValidationFailure(f"invalid value for {name}") from exc


async def _put(db: Database, key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO settings_overrides(key, value_json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at",
        (key, json.dumps(value), utcnow_iso()),
    )


async def _settings_view(state: State) -> dict[str, Any]:
    s = state.settings
    runtime = state.runtime
    values = {name: getattr(s, name) for name in _EXPOSED}
    manager = runtime.services.memory
    return {
        "values": values,
        "mutable": list(MUTABLE),
        "keys_present": s.key_presence(),  # booleans only, never a key
        "routes": runtime.router.capability_matrix(),
        "routing_problems": runtime.router.check_roles(),
        "tools": sorted(runtime.registry.names()),
        "disabled_tools": sorted(runtime.agent.disabled_tools),
        "muted_categories": await manager.muted() if manager else [],
        "charter_sha256": runtime.pack.charter_sha256,
        "canon_through": runtime.pack.canon_through,
        "storage": await runtime.files.store.storage_use(),
    }


@router.get("/settings")
async def get_settings(session: Authed, state: State) -> dict[str, Any]:
    """Settings for the Settings pages. Never returns keys or secrets."""
    return await _settings_view(state)


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, session: Authed, state: State) -> dict[str, Any]:
    """Change runtime settings and persist them."""
    runtime = state.runtime
    sent = body.model_dump(exclude_unset=True)
    known = set(runtime.registry.names())
    for name, value in sent.items():
        if name == "disabled_tools":
            unknown = set(value or []) - known
            if unknown:
                raise ValidationFailure(f"unknown tools: {sorted(unknown)}")
            disabled = runtime.agent.disabled_tools
            disabled.clear()
            disabled.update(value or [])
            await _put(runtime.db, "disabled_tools", sorted(disabled))
            continue
        checked = _validate(name, value)
        object.__setattr__(state.settings, name, checked)
        await _put(runtime.db, f"setting:{name}", checked)
    await runtime.db.audit("settings.changed", "operator", {"fields": sorted(sent)})
    return await _settings_view(state)


@router.get("/status")
async def status(session: Authed, state: State) -> dict[str, Any]:
    """Header data: model, vault size, guard health, a small host snapshot."""
    runtime = state.runtime
    vault = runtime.services.vault
    chain = [runtime.router.route_for("main"), *map(parse_route, state.settings.fallback_chain)]
    reachable = [r for r in chain if runtime.router.capabilities_of(r) is not None]
    main = reachable[0] if reachable else chain[0]  # what will actually answer
    guard = runtime.router.route_for("guard")
    matrix = runtime.router.capability_matrix()
    try:
        telemetry = await asyncio.wait_for(
            asyncio.to_thread(
                collect, TelemetryInput(sections=["cpu", "memory", "uptime"], top_n=1)
            ),
            5,
        )
        host: dict[str, Any] | None = telemetry.model_dump(mode="json", exclude_none=True)
    except (TimeoutError, OSError):
        host = None
    return {
        "provider": main.provider,
        "model": main.model,
        "main_available": bool(reachable),
        "vault_count": await vault.count() if vault else 0,
        "guard": {
            "route": str(guard),
            "available": matrix["guard"]["available"],
            "status": "ok" if matrix["guard"]["available"] else "heuristic_only",
        },
        "remote": session.is_remote,
        "access_mode": state.settings.access_mode,
        "strict": state.settings.quarantine_mode == "strict",
        "host": host,
        "budget": {
            "daily_usd": state.settings.daily_budget_usd,
            "spent_today_usd": round(await runtime.router.budget.spent_today(), 4),
        },
    }


@router.get("/redteam/latest")
async def redteam_latest(session: Authed, state: State) -> dict[str, Any]:
    """The most recent red-team run, or ``{"run": null}`` before the first."""
    row = await state.runtime.db.fetchone(
        "SELECT id, started_at, finished_at, summary_json FROM redteam_runs "
        "ORDER BY started_at DESC LIMIT 1"
    )
    if row is None:
        return {"run": None}
    return {
        "run": {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "summary": from_json(row["summary_json"], {}),
        }
    }


@router.get("/safety")
async def safety(session: Authed, state: State, limit: int = 50) -> dict[str, Any]:
    """Guard decisions, egress log, drift scores, and canary events for the Safety page."""
    db = state.runtime.db
    n = max(1, min(limit, 200))
    decisions = await db.fetchall(
        "SELECT chat_id, turn_id, class, attack_type, confidence, action, task_type, source, "
        "created_at FROM guard_decisions ORDER BY created_at DESC LIMIT ?",
        (n,),
    )
    counts = await db.fetchall(
        "SELECT class, COUNT(*) AS n FROM guard_decisions GROUP BY class ORDER BY n DESC"
    )
    egress = await db.fetchall(
        "SELECT chat_id, turn_id, call_id, tool, decision, reason, created_at FROM egress_log "
        "ORDER BY created_at DESC LIMIT ?",
        (n,),
    )
    drift = await db.fetchall(
        "SELECT chat_id, score, created_at FROM drift_scores ORDER BY created_at DESC LIMIT ?",
        (n,),
    )
    canary = await db.fetchall(
        "SELECT event, payload_json, created_at FROM audit_log WHERE event LIKE 'canary%' "
        "OR event LIKE 'output.blocked%' ORDER BY created_at DESC LIMIT ?",
        (n,),
    )
    return {
        "decisions": decisions,
        "class_counts": {r["class"]: r["n"] for r in counts},
        "egress": egress,
        "drift": drift,
        "canary_events": [
            {
                "event": r["event"],
                "created_at": r["created_at"],
                "payload": from_json(r["payload_json"], {}),
            }
            for r in canary
        ],
    }
