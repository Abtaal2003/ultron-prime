"""Memory Vault routes: the operator's view and the consent decision (spec 6.4, 9)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from src.api.deps import Authed, State
from src.core.errors import NotFound

router = APIRouter(tags=["memory"])


class ProposalDecision(BaseModel):
    """Approve, edit and approve, or decline a proposed memory."""

    decision: str
    edited_text: Annotated[str, Field(max_length=2000)] | None = None
    mute_category: bool = False


class MemoryPatch(BaseModel):
    """Operator edit of a stored memory."""

    text: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    category: str | None = None
    importance: Annotated[int, Field(ge=1, le=5)] | None = None


def _vault(state: State) -> Any:
    assert state.runtime.services.vault is not None
    return state.runtime.services.vault


def _manager(state: State) -> Any:
    assert state.runtime.services.memory is not None
    return state.runtime.services.memory


@router.get("/memory")
async def list_memories(
    session: Authed,
    state: State,
    category: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Active memories, newest first."""
    vault = _vault(state)
    items = await vault.list_items(category=category, text_filter=q, limit=limit, offset=offset)
    return {"items": [i.to_dict() for i in items], "total": await vault.count()}


@router.get("/memory/proposals")
async def pending_proposals(session: Authed, state: State) -> dict[str, Any]:
    """Proposals awaiting a decision, plus muted categories."""
    manager = _manager(state)
    return {"pending": await manager.pending(), "muted": await manager.muted()}


@router.get("/memory/proposals/log")
async def proposals_log(
    session: Authed, state: State, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[dict[str, Any]]:
    """Recent proposals with their verdicts (no memory text)."""
    rows: list[dict[str, Any]] = await _manager(state).proposals_log(limit)
    return rows


@router.post("/memory/proposals/{proposal_id}/decision")
async def decide(
    proposal_id: str, body: ProposalDecision, session: Authed, state: State
) -> dict[str, Any]:
    """Apply the operator's consent decision."""
    result = await _manager(state).decide(
        proposal_id,
        body.decision,
        edited_text=body.edited_text,
        mute_category=body.mute_category,
    )
    return {"status": result.status, "memory": result.memory.to_dict() if result.memory else None}


@router.post("/memory/unmute")
async def unmute(category: str, session: Authed, state: State) -> dict[str, bool]:
    """Allow proposals in a muted category again."""
    await _manager(state).unmute(category)
    return {"ok": True}


@router.patch("/memory/{memory_id}")
async def patch_memory(
    memory_id: str, body: MemoryPatch, session: Authed, state: State
) -> dict[str, Any]:
    """Edit a memory."""
    item = await _vault(state).update(
        memory_id, text=body.text, category=body.category, importance=body.importance
    )
    return dict(item.to_dict())


@router.delete("/memory/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, session: Authed, state: State) -> Response:
    """Hard-delete a memory."""
    if not await _vault(state).delete(memory_id):
        raise NotFound("no such memory")
    return Response(status_code=204)


@router.get("/memory-export")
async def export_memories(session: Authed, state: State) -> list[dict[str, Any]]:
    """Every active memory, for the operator's own backup."""
    rows: list[dict[str, Any]] = await _vault(state).export()
    return rows
