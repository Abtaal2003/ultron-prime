"""Turn routes: start (multipart), stream (SSE), stop, egress decisions (spec 6.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from src.api.auth import is_loopback
from src.api.deps import Authed, State
from src.core.errors import LimitExceeded, NotFound, ValidationFailure
from src.files.ingest import check_batch

router = APIRouter(tags=["turns"])
MAX_TEXT_CHARS = 20_000
_CHUNK = 1024 * 1024


class DecisionBody(BaseModel):
    """Operator choice for a held call."""

    decision: str


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Read an upload in chunks, stopping as soon as it exceeds ``cap`` bytes."""
    data = bytearray()
    while chunk := await upload.read(_CHUNK):
        data.extend(chunk)
        if len(data) > cap:
            raise LimitExceeded(f"'{upload.filename}' is larger than {cap // (1024 * 1024)} MB")
    return bytes(data)


@router.post("/chats/{chat_id}/turns", status_code=202)
async def start_turn(
    chat_id: str,
    request: Request,
    session: Authed,
    state: State,
    text: Annotated[str, Form(max_length=MAX_TEXT_CHARS)] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> dict[str, Any]:
    """Start a turn from text and up to five files. Returns at once with the turn id."""
    settings = state.settings
    uploads = [f for f in (files or []) if f.filename]
    if not text.strip() and not uploads:
        raise ValidationFailure("send some text or a file")
    agent = state.runtime.agent
    if agent.is_busy(chat_id):  # checked before any file is stored
        from src.core.errors import Conflict

        raise Conflict("a turn is already running in this chat")
    blobs: list[tuple[str, bytes]] = []
    for upload in uploads:
        blobs.append(
            (
                upload.filename or "file",
                await _read_capped(upload, settings.upload_max_mb * 1024 * 1024),
            )
        )
    check_batch(settings, [len(b) for _, b in blobs])
    ids: list[str] = []
    ingested: list[dict[str, Any]] = []
    try:
        for name, data in blobs:
            result = await state.runtime.files.ingest(chat_id=chat_id, filename=name, data=data)
            ids.append(result.record.id)
            ingested.append(
                {
                    "id": result.record.id,
                    "name": result.record.original_name,
                    "family": result.family,
                    "warnings": result.warnings,
                    "quarantine": result.quarantine,
                }
            )
    except Exception:
        for att_id in ids:
            await state.runtime.db.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        raise
    client = request.client.host if request.client else None
    remote = session.is_remote or not is_loopback(client)
    turn_id = await agent.start_turn(chat_id, text, attachment_ids=ids, is_remote=remote)
    return {"turn_id": turn_id, "attachments": ingested}


@router.get("/turns/{turn_id}/events")
async def turn_events(
    turn_id: str,
    request: Request,
    session: Authed,
    state: State,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> EventSourceResponse:
    """Server-sent events for one turn; resumes after ``Last-Event-ID`` (or ``?after=``)."""
    row = await state.runtime.db.fetchone("SELECT id FROM turns WHERE id=?", (turn_id,))
    if row is None:
        raise NotFound("turn not found")
    start = after or 0
    if last_event_id and last_event_id.isdigit():
        start = max(start, int(last_event_id))

    async def stream() -> AsyncIterator[ServerSentEvent]:
        async for event in state.runtime.bus.subscribe(turn_id, start):
            yield ServerSentEvent(data=event.model_dump_json(), event=event.type, id=str(event.seq))

    return EventSourceResponse(
        stream(),
        ping=int(state.settings.sse_heartbeat_s),
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/turns/{turn_id}/stop")
async def stop_turn(turn_id: str, session: Authed, state: State) -> dict[str, bool]:
    """Cancel a running turn (never inferred from a disconnect)."""
    return {"stopped": await state.runtime.agent.stop_turn(turn_id)}


@router.post("/egress/{call_id}/decision")
async def egress_decision(
    call_id: str, body: DecisionBody, session: Authed, state: State
) -> dict[str, bool]:
    """Approve or reject a held tool call."""
    if not state.runtime.agent.resolve_hold(call_id, body.decision):
        raise NotFound("no pending hold with that id")
    return {"ok": True}


@router.get("/turns/{turn_id}/quarantine/{handle}")
async def quarantine_original(
    turn_id: str, handle: str, session: Authed, state: State
) -> dict[str, str]:
    """The original behind a quarantine handle, for the operator only ("show original")."""
    raw = state.runtime.agent.quarantine_original(turn_id, handle)
    if raw is None:
        raise NotFound("that original is no longer held in memory")
    return {"handle": handle, "text": raw}
