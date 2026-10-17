"""Workshop routes: files, versions, downloads, safe previews, attachment trust (spec 6.4, 6.5).

Downloads are always ``attachment``. Previews serve only re-encoded raster images (with
``Content-Security-Policy: sandbox``) or JSON; uploaded or generated HTML and SVG are never
served inline.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from src.api.deps import Authed, State
from src.core.errors import NotFound
from src.files.store import VersionRecord

router = APIRouter(tags=["files"])
SAFE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
TEXT_PREVIEW_BYTES = 200_000
_TEXT_SUFFIX = (
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".yaml",
    ".yml",
    ".toml",
    ".log",
    ".html",
    ".svg",
    ".xml",
    ".sh",
    ".sql",
    ".css",
)


class TrustBody(BaseModel):
    """Attachment trust toggle."""

    trust: str


def _disposition(name: str) -> str:
    safe = name.replace('"', "").replace("\\", "")
    return (
        f'attachment; filename="{safe.encode("ascii", "ignore").decode() or "download"}"; '
        f"filename*=UTF-8''{quote(name)}"
    )


async def _version(state: State, file_id: str, version: int) -> VersionRecord:
    rec = await state.runtime.files.store.get_version(file_id, version)
    if rec is None:
        raise NotFound("file version not found")
    return rec


def _version_out(v: VersionRecord) -> dict[str, Any]:
    return {
        "file_id": v.file_id,
        "logical_name": v.logical_name,
        "kind": v.kind,
        "version": v.version,
        "sha256": v.sha256,
        "mime": v.mime,
        "size": v.size_bytes,
        "created_by": v.created_by,
        "derived_from": v.derived_from,
        "sources": v.sources,
        "external_content": v.external_content,
        "change_note": v.change_note,
        "created_at": v.created_at,
        "chat_id": v.chat_id,
    }


@router.get("/files")
async def list_files(
    session: Authed, state: State, chat: Annotated[str | None, Query()] = None
) -> list[dict[str, Any]]:
    """Files the agent has created or modified (newest first)."""
    return await state.runtime.files.store.list_files(chat)


@router.get("/files/{file_id}/versions")
async def list_versions(file_id: str, session: Authed, state: State) -> list[dict[str, Any]]:
    """Every immutable version of a file."""
    versions = await state.runtime.files.store.list_versions(file_id)
    if not versions:
        raise NotFound("file not found")
    return [_version_out(v) for v in versions]


@router.get("/files/{file_id}/versions/{version}/download")
async def download(file_id: str, version: int, session: Authed, state: State) -> Response:
    """Download one version as an attachment (hash verified)."""
    rec = await _version(state, file_id, version)
    data = await state.runtime.files.store.read_version_bytes(file_id, version)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _disposition(rec.logical_name),
            "X-Content-SHA256": rec.sha256,
            "Content-Security-Policy": "sandbox",
        },
    )


@router.get("/files/{file_id}/versions/{version}/preview", response_model=None)
async def preview(file_id: str, version: int, session: Authed, state: State) -> Response:
    """A safe preview: a raster image, text as JSON, or metadata only."""
    rec = await _version(state, file_id, version)
    data = await state.runtime.files.store.read_version_bytes(file_id, version)
    sandbox = {"Content-Security-Policy": "sandbox"}
    if rec.mime in SAFE_IMAGE_MIMES:
        return Response(data, media_type=rec.mime, headers=sandbox)
    if (
        rec.mime.startswith("text/")
        or rec.logical_name.lower().endswith(_TEXT_SUFFIX)
        or rec.mime in {"application/json"}
    ):
        text = data[:TEXT_PREVIEW_BYTES].decode("utf-8", errors="replace")
        return JSONResponse(
            {
                "kind": "text",
                "text": text,
                "truncated": len(data) > TEXT_PREVIEW_BYTES,
                "name": rec.logical_name,
            },
            headers=sandbox,
        )
    return JSONResponse(
        {
            "kind": "binary",
            "name": rec.logical_name,
            "mime": rec.mime,
            "size": rec.size_bytes,
            "note": "no inline preview; download to open",
        },
        headers=sandbox,
    )


@router.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, session: Authed, state: State) -> dict[str, Any]:
    """Attachment metadata."""
    rec = await state.runtime.files.store.get_attachment(attachment_id)
    if rec is None:
        raise NotFound("attachment not found")
    return {
        "id": rec.id,
        "chat_id": rec.chat_id,
        "name": rec.original_name,
        "mime": rec.detected_mime,
        "size": rec.size_bytes,
        "sha256": rec.sha256,
        "pages": rec.pages,
        "trust": rec.trust,
        "status": rec.status,
        "secret_warning": rec.secret_warning,
        "created_at": rec.created_at,
    }


@router.get("/attachments/{attachment_id}/content")
async def attachment_content(attachment_id: str, session: Authed, state: State) -> Response:
    """Download the stored original of an attachment (always as an attachment)."""
    store = state.runtime.files.store
    rec = await store.get_attachment(attachment_id)
    if rec is None:
        raise NotFound("attachment not found")
    data = store.attachment_path(rec).read_bytes()
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _disposition(rec.original_name),
            "Content-Security-Policy": "sandbox",
        },
    )


@router.get("/attachments/{attachment_id}/thumbnail", response_model=None)
async def attachment_thumbnail(attachment_id: str, session: Authed, state: State) -> Response:
    """The re-encoded raster image of an image attachment (anything else is a 404)."""
    store = state.runtime.files.store
    rec = await store.get_attachment(attachment_id)
    if rec is None or rec.detected_mime not in SAFE_IMAGE_MIMES:
        raise NotFound("no image preview for this attachment")
    return Response(
        store.attachment_path(rec).read_bytes(),
        media_type=rec.detected_mime,
        headers={"Content-Security-Policy": "sandbox", "Cache-Control": "private, max-age=3600"},
    )


@router.patch("/attachments/{attachment_id}")
async def set_trust(
    attachment_id: str, body: TrustBody, session: Authed, state: State
) -> dict[str, str]:
    """Mark an attachment trusted or external (external is read through the quarantined reader)."""
    await state.runtime.files.store.set_trust(attachment_id, body.trust)
    return {"id": attachment_id, "trust": body.trust}
