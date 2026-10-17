"""Chat sidebar and message history routes (spec 6.4)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from src.api.deps import Authed, State
from src.core.errors import NotFound

router = APIRouter(prefix="/chats", tags=["chats"])


class ChatCreate(BaseModel):
    """New chat request."""

    title: Annotated[str, Field(max_length=200)] = "New chat"
    private: bool = False


class ChatPatch(BaseModel):
    """Chat update; only the fields sent are changed."""

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    archived: bool | None = None
    private: bool | None = None


def _chat_out(row: dict[str, Any], active_turn: str | None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": bool(row["archived"]),
        "private": bool(row["private"]),
        "message_count": row.get("message_count"),
        "active_turn": active_turn,
    }


async def _require(state: State, chat_id: str) -> dict[str, Any]:
    assert state.runtime.services.history is not None
    row = await state.runtime.services.history.get_chat(chat_id)
    if row is None:
        raise NotFound("chat not found")
    return row


@router.get("")
async def list_chats(
    session: Authed,
    state: State,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """Sidebar listing, newest first."""
    history = state.runtime.services.history
    assert history is not None
    rows = await history.list_chats(include_archived=archived, limit=limit, offset=offset)
    agent = state.runtime.agent
    return [_chat_out(r, agent.active_turn(r["id"])) for r in rows]


@router.post("", status_code=201)
async def create_chat(body: ChatCreate, session: Authed, state: State) -> dict[str, Any]:
    """Create a chat."""
    history = state.runtime.services.history
    assert history is not None
    chat_id = await history.create_chat(body.title, private=body.private)
    return _chat_out(await _require(state, chat_id), None)


@router.post("/purge-archived")
async def purge_archived(session: Authed, state: State) -> dict[str, int]:
    """Permanently delete every archived chat (messages, vectors, attachments)."""
    history = state.runtime.services.history
    assert history is not None
    rows = await history.list_chats(include_archived=True, limit=10_000)
    victims = [r["id"] for r in rows if r["archived"]]
    for chat_id in victims:
        await state.runtime.files.store.purge_chat_attachments(chat_id)
        await history.delete_chat(chat_id)
    await state.runtime.db.audit("chats.purge_archived", "operator", {"count": len(victims)})
    return {"deleted": len(victims)}


@router.get("/{chat_id}/export")
async def export_chat(chat_id: str, session: Authed, state: State) -> Response:
    """The transcript as Markdown (an attachment)."""
    chat = await _require(state, chat_id)
    history = state.runtime.services.history
    assert history is not None
    lines = [
        f"# {chat['title']}",
        "",
        f"_Exported from Ultron Prime. Created {chat['created_at']}._",
        "",
    ]
    for m in await history.get_messages(chat_id):
        if m["role"] not in {"user", "assistant"}:
            continue
        who = "Operator" if m["role"] == "user" else "Ultron"
        lines += [f"## {who} ({m['created_at']})", "", m["content"], ""]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in chat["title"])[:60] or "chat"
    return Response(
        "\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.md"',
            "Content-Security-Policy": "sandbox",
        },
    )


@router.get("/{chat_id}")
async def get_chat(chat_id: str, session: Authed, state: State) -> dict[str, Any]:
    """One chat, including the id of its running turn so a reload can resume the stream."""
    row = await _require(state, chat_id)
    return _chat_out(row, state.runtime.agent.active_turn(chat_id))


@router.patch("/{chat_id}")
async def patch_chat(
    chat_id: str, body: ChatPatch, session: Authed, state: State
) -> dict[str, Any]:
    """Rename, archive, or toggle the private flag."""
    await _require(state, chat_id)
    history = state.runtime.services.history
    assert history is not None
    if body.title is not None:
        await history.rename_chat(chat_id, body.title)
    if body.archived is not None:
        await history.set_archived(chat_id, body.archived)
    if body.private is not None:
        await history.set_private(chat_id, body.private)
    return _chat_out(await _require(state, chat_id), state.runtime.agent.active_turn(chat_id))


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(chat_id: str, session: Authed, state: State) -> Response:
    """Delete a chat, its messages, vectors, and attachments."""
    await _require(state, chat_id)
    agent = state.runtime.agent
    active = agent.active_turn(chat_id)
    if active:
        await agent.stop_turn(active)
    history = state.runtime.services.history
    assert history is not None
    await state.runtime.files.store.purge_chat_attachments(chat_id)
    await history.delete_chat(chat_id)
    return Response(status_code=204)


@router.get("/{chat_id}/messages")
async def list_messages(
    chat_id: str,
    session: Authed,
    state: State,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Paged history, oldest first within the page; ``before`` is a message id."""
    await _require(state, chat_id)
    history = state.runtime.services.history
    assert history is not None
    if before:
        anchor = await history.get_message(before)
        if anchor is None or anchor["chat_id"] != chat_id:
            raise NotFound("message not found")
    rows = await history.get_messages(chat_id, limit=limit + 1, before=before)
    has_more = len(rows) > limit
    page = rows[-limit:] if has_more else rows
    for r in page:
        r.pop("embedding", None)
    return {"messages": page, "has_more": has_more}
