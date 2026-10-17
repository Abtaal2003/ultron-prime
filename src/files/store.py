"""Attachment store and versioned file store (spec 10.3, 11.9, 11.10).

* **Attachments** are immutable, content-addressed originals in ``data/attachments/<sha256>.<ext>``.
* **Files** are logical files with ordered **versions**. Every version's bytes live in
  ``data/versions/<file_id>/v<N>_<ext>``; the *head* (latest) is mirrored to the workspace so
  the operator can open it in their own editor. Nothing is ever deleted (no delete in v1).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import Settings
from src.core.db import Database, from_json, new_id, to_json
from src.core.errors import Conflict, LimitExceeded, NotFound, PolicyDenied
from src.core.events import utcnow_iso
from src.files.types import extension_of
from src.tools.base import FileRef
from src.tools.security import confine_path

CreatedBy = str  # 'operator' | 'forge' | 'scribe' | 'sandbox'


@dataclass(slots=True)
class AttachmentRecord:
    """One row of ``attachments``."""

    id: str
    chat_id: str
    message_id: str | None
    original_name: str
    safe_name: str
    detected_mime: str
    size_bytes: int
    sha256: str
    pages: int | None
    width: int | None
    height: int | None
    trust: str
    status: str
    secret_warning: bool
    stored_path: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AttachmentRecord:
        """Build from a database row."""
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            original_name=row["original_name"],
            safe_name=row["safe_name"],
            detected_mime=row["detected_mime"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            pages=row["pages"],
            width=row["width"],
            height=row["height"],
            trust=row["trust"],
            status=row["status"],
            secret_warning=bool(row["secret_warning"]),
            stored_path=row["stored_path"],
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class VersionRecord:
    """One row of ``file_versions`` joined with its logical file."""

    id: str
    file_id: str
    logical_name: str
    kind: str
    chat_id: str | None
    version: int
    path: str
    sha256: str
    mime: str
    size_bytes: int
    created_by: str
    derived_from: list[str]
    sources: list[str]
    external_content: bool
    change_note: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> VersionRecord:
        """Build from a joined row."""
        return cls(
            id=row["id"],
            file_id=row["file_id"],
            logical_name=row["logical_name"],
            kind=row["kind"],
            chat_id=row["chat_id"],
            version=row["version"],
            path=row["path"],
            sha256=row["sha256"],
            mime=row["mime"],
            size_bytes=row["size_bytes"],
            created_by=row["created_by"],
            derived_from=from_json(row["derived_from_json"], []),
            sources=from_json(row["sources_json"], []),
            external_content=bool(row["external_content"]),
            change_note=row["change_note"],
            created_at=row["created_at"],
        )

    def to_ref(self) -> FileRef:
        """Convert to the tool-facing :class:`FileRef`."""
        return FileRef(
            file_id=self.file_id,
            version=self.version,
            path=self.path,
            logical_name=self.logical_name,
            sha256=self.sha256,
            mime=self.mime,
            size=self.size_bytes,
            external_content=self.external_content,
            derived_from=self.derived_from,
            change_note=self.change_note,
        )


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def atomic_write(target: Path, data: bytes, *, read_only: bool = False) -> None:
    """Write ``data`` to ``target`` via a temp file and ``os.replace`` (no half-written files)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if read_only:
            tmp.chmod(0o444)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class FileStore:
    """Persistence for attachments and versioned files."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.attachments_dir = settings.attachments_dir
        self.versions_dir = settings.data_dir / "versions"
        self.workspace = settings.workspace_dir
        self._write_lock = asyncio.Lock()

    # ----------------------------------------------------------------- attachments --------
    async def save_attachment(
        self,
        *,
        chat_id: str,
        original_name: str,
        safe_name: str,
        detected_mime: str,
        data: bytes,
        message_id: str | None = None,
        pages: int | None = None,
        width: int | None = None,
        height: int | None = None,
        secret_warning: bool = False,
        status: str = "accepted",
    ) -> AttachmentRecord:
        """Store an accepted upload's (already sanitised) bytes immutably and record it."""
        digest = sha256_bytes(data)
        ext = extension_of(safe_name) or ".bin"
        target = self.attachments_dir / f"{digest}{ext}"
        if not target.exists():
            atomic_write(target, data, read_only=True)
        record_id = new_id("att")
        created = utcnow_iso()
        await self.db.execute(
            "INSERT INTO attachments(id, chat_id, message_id, original_name, safe_name, "
            "detected_mime, size_bytes, sha256, pages, width, height, trust, status, "
            "secret_warning, stored_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                chat_id,
                message_id,
                original_name,
                safe_name,
                detected_mime,
                len(data),
                digest,
                pages,
                width,
                height,
                "external",
                status,
                int(secret_warning),
                target.name,
                created,
            ),
        )
        rec = await self.get_attachment(record_id)
        assert rec is not None
        return rec

    async def get_attachment(self, attachment_id: str) -> AttachmentRecord | None:
        """Return an attachment by id."""
        row = await self.db.fetchone("SELECT * FROM attachments WHERE id=?", (attachment_id,))
        return AttachmentRecord.from_row(row) if row else None

    async def list_attachments(self, chat_id: str) -> list[AttachmentRecord]:
        """Return a chat's attachments, oldest first."""
        rows = await self.db.fetchall(
            "SELECT * FROM attachments WHERE chat_id=? ORDER BY created_at", (chat_id,)
        )
        return [AttachmentRecord.from_row(r) for r in rows]

    async def attach_to_message(self, attachment_id: str, message_id: str) -> None:
        """Link an attachment to the message that carried it."""
        await self.db.execute(
            "UPDATE attachments SET message_id=? WHERE id=?", (message_id, attachment_id)
        )

    async def set_trust(self, attachment_id: str, trust: str) -> None:
        """Mark an attachment ``trusted`` or ``external`` (skips quarantine when trusted)."""
        if trust not in {"trusted", "external"}:
            raise PolicyDenied("trust must be 'trusted' or 'external'")
        if await self.get_attachment(attachment_id) is None:
            raise NotFound("attachment not found")
        await self.db.execute("UPDATE attachments SET trust=? WHERE id=?", (trust, attachment_id))
        await self.db.audit("attachment.trust", "operator", {"id": attachment_id, "trust": trust})

    def attachment_path(self, record: AttachmentRecord) -> Path:
        """Return the absolute path of the stored original."""
        if not record.stored_path:
            raise NotFound("attachment has no stored bytes")
        return confine_path(self.attachments_dir, record.stored_path)

    async def add_chunks(
        self, attachment_id: str, chunks: list[tuple[int | None, int, int, str]]
    ) -> list[str]:
        """Store ``(page, span_start, span_end, text)`` chunks; returns their ids."""
        ids = [new_id("chk") for _ in chunks]
        async with self.db.transaction() as tx:
            await tx.executemany(
                "INSERT INTO attachment_chunks(id, attachment_id, page, span_start, span_end, text)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (cid, attachment_id, p, s, e, t)
                    for cid, (p, s, e, t) in zip(ids, chunks, strict=True)
                ],
            )
        return ids

    async def get_chunks(
        self, attachment_id: str, pages: list[int] | None = None
    ) -> list[dict[str, Any]]:
        """Return stored chunks (optionally limited to ``pages``), in order."""
        if pages:
            marks = ",".join("?" for _ in pages)
            return await self.db.fetchall(
                f"SELECT * FROM attachment_chunks WHERE attachment_id=? AND page IN ({marks}) "  # noqa: S608
                "ORDER BY page, span_start",
                (attachment_id, *pages),
            )
        return await self.db.fetchall(
            "SELECT * FROM attachment_chunks WHERE attachment_id=? ORDER BY page, span_start",
            (attachment_id,),
        )

    async def storage_use(self, chat_id: str | None = None) -> dict[str, int]:
        """Return attachment and file version storage totals (bytes)."""
        if chat_id:
            att = await self.db.scalar(
                "SELECT COALESCE(SUM(size_bytes),0) FROM attachments WHERE chat_id=?", (chat_id,)
            )
            ver = await self.db.scalar(
                "SELECT COALESCE(SUM(size_bytes),0) FROM file_versions WHERE chat_id=?", (chat_id,)
            )
        else:
            att = await self.db.scalar("SELECT COALESCE(SUM(size_bytes),0) FROM attachments")
            ver = await self.db.scalar("SELECT COALESCE(SUM(size_bytes),0) FROM file_versions")
        return {"attachments_bytes": int(att or 0), "versions_bytes": int(ver or 0)}

    async def purge_chat_attachments(self, chat_id: str) -> int:
        """Delete a chat's attachment rows and any originals no other row references."""
        rows = await self.db.fetchall(
            "SELECT id, stored_path FROM attachments WHERE chat_id=?", (chat_id,)
        )
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM attachments WHERE chat_id=?", (chat_id,))
        for row in rows:
            stored = row["stored_path"]
            if stored and not await self.db.scalar(
                "SELECT COUNT(*) FROM attachments WHERE stored_path=?", (stored,)
            ):
                path = self.attachments_dir / stored
                if path.exists():
                    path.chmod(0o644)
                    path.unlink(missing_ok=True)
        await self.db.audit(
            "attachments.purge", "operator", {"chat_id": chat_id, "count": len(rows)}
        )
        return len(rows)

    # ----------------------------------------------------------------- versioned files ----
    def _version_path(self, file_id: str, version: int, name: str) -> Path:
        ext = extension_of(name)
        return self.versions_dir / file_id / f"v{version}{ext}"

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        """Return a ``files`` row."""
        return await self.db.fetchone("SELECT * FROM files WHERE id=?", (file_id,))

    async def find_file(self, logical_name: str) -> dict[str, Any] | None:
        """Return the logical file whose head lives at ``logical_name``."""
        return await self.db.fetchone("SELECT * FROM files WHERE logical_name=?", (logical_name,))

    async def write_version(
        self,
        *,
        logical_name: str,
        data: bytes,
        mime: str,
        created_by: CreatedBy,
        chat_id: str | None,
        kind: str = "generated",
        derived_from: list[str] | None = None,
        sources: list[str] | None = None,
        external_content: bool = False,
        change_note: str | None = None,
        max_bytes: int | None = None,
        mirror_head: bool = True,
    ) -> VersionRecord:
        """Append a new version of ``logical_name`` (creating the file on first use).

        If the head file already exists in the workspace but was never versioned (for example the
        operator's own file), its current bytes are first snapshotted as version 1 so nothing is
        lost. The head mirror is written last, atomically.
        """
        async with self._write_lock:
            if max_bytes is not None and len(data) > max_bytes:
                raise LimitExceeded(f"file exceeds the {max_bytes // (1024 * 1024)} MB limit")
            head = confine_path(self.workspace, logical_name)
            file_row = await self.find_file(logical_name)
            now = utcnow_iso()
            if file_row is None:
                file_id = new_id("file")
                await self.db.execute(
                    "INSERT INTO files(id, chat_id, logical_name, kind, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (file_id, chat_id, logical_name, kind, now),
                )
                next_version = 1
                if head.is_file():
                    prior = head.read_bytes()
                    await self._insert_version(
                        file_id=file_id,
                        chat_id=chat_id,
                        version=1,
                        logical_name=logical_name,
                        data=prior,
                        mime=mime,
                        created_by="operator",
                        derived_from=[],
                        sources=[],
                        external_content=False,
                        change_note="existing file snapshotted before change",
                    )
                    next_version = 2
            else:
                file_id = file_row["id"]
                current = await self.db.scalar(
                    "SELECT COALESCE(MAX(version),0) FROM file_versions WHERE file_id=?", (file_id,)
                )
                next_version = int(current) + 1
                # Keep bytes edited outside Ultron since the last version.
                if head.is_file():
                    last = await self.db.fetchone(
                        "SELECT sha256 FROM file_versions WHERE file_id=? "
                        "ORDER BY version DESC LIMIT 1",
                        (file_id,),
                    )
                    prior = head.read_bytes()
                    if last is not None and sha256_bytes(prior) != last["sha256"]:
                        await self._insert_version(
                            file_id=file_id,
                            chat_id=chat_id,
                            version=next_version,
                            logical_name=logical_name,
                            data=prior,
                            mime=mime,
                            created_by="operator",
                            derived_from=[],
                            sources=[],
                            external_content=False,
                            change_note="operator edit outside Ultron snapshotted",
                        )
                        next_version += 1
            record = await self._insert_version(
                file_id=file_id,
                chat_id=chat_id,
                version=next_version,
                logical_name=logical_name,
                data=data,
                mime=mime,
                created_by=created_by,
                derived_from=derived_from or [],
                sources=sources or [],
                external_content=external_content,
                change_note=change_note,
            )
            if mirror_head:
                atomic_write(head, data)
            return record

    async def _insert_version(
        self,
        *,
        file_id: str,
        chat_id: str | None,
        version: int,
        logical_name: str,
        data: bytes,
        mime: str,
        created_by: str,
        derived_from: list[str],
        sources: list[str],
        external_content: bool,
        change_note: str | None,
    ) -> VersionRecord:
        blob = self._version_path(file_id, version, logical_name)
        atomic_write(blob, data, read_only=True)
        try:
            await self.db.execute(
                "INSERT INTO file_versions(id, file_id, chat_id, version, path, sha256, mime, "
                "size_bytes, created_by, derived_from_json, sources_json, external_content, "
                "change_note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("ver"),
                    file_id,
                    chat_id,
                    version,
                    blob.relative_to(self.settings.data_dir).as_posix(),
                    sha256_bytes(data),
                    mime,
                    len(data),
                    created_by,
                    to_json(derived_from),
                    to_json(sources),
                    int(external_content),
                    change_note,
                    utcnow_iso(),
                ),
            )
        except BaseException:
            blob.chmod(0o644)
            blob.unlink(missing_ok=True)
            raise
        rec = await self.get_version(file_id, version)
        assert rec is not None
        return rec

    async def get_version(self, file_id: str, version: int) -> VersionRecord | None:
        """Return one version joined with its file."""
        row = await self.db.fetchone(
            "SELECT v.*, f.logical_name, f.kind FROM file_versions v "
            "JOIN files f ON f.id=v.file_id "
            "WHERE v.file_id=? AND v.version=?",
            (file_id, version),
        )
        return VersionRecord.from_row(row) if row else None

    async def latest_version(self, file_id: str) -> VersionRecord | None:
        """Return the newest version of a file."""
        row = await self.db.fetchone(
            "SELECT v.*, f.logical_name, f.kind FROM file_versions v "
            "JOIN files f ON f.id=v.file_id "
            "WHERE v.file_id=? ORDER BY v.version DESC LIMIT 1",
            (file_id,),
        )
        return VersionRecord.from_row(row) if row else None

    async def list_versions(self, file_id: str) -> list[VersionRecord]:
        """Return every version of a file, oldest first."""
        rows = await self.db.fetchall(
            "SELECT v.*, f.logical_name, f.kind FROM file_versions v "
            "JOIN files f ON f.id=v.file_id "
            "WHERE v.file_id=? ORDER BY v.version",
            (file_id,),
        )
        return [VersionRecord.from_row(r) for r in rows]

    async def list_files(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        """Return logical files with their newest version summary (Workshop listing)."""
        if chat_id is None:
            rows = await self.db.fetchall("SELECT id FROM files ORDER BY created_at DESC")
        else:
            rows = await self.db.fetchall(
                "SELECT DISTINCT f.id FROM files f JOIN file_versions v ON v.file_id=f.id "
                "WHERE v.chat_id=? AND v.created_by != 'operator' ORDER BY f.created_at DESC",
                (chat_id,),
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            versions = await self.list_versions(row["id"])
            if not versions:
                continue
            latest = versions[-1]
            out.append(
                {
                    "file_id": row["id"],
                    "logical_name": latest.logical_name,
                    "kind": latest.kind,
                    "latest_version": latest.version,
                    "mime": latest.mime,
                    "size": latest.size_bytes,
                    "sha256": latest.sha256,
                    "external_content": any(v.external_content for v in versions),
                    "version_count": len(versions),
                    "updated_at": latest.created_at,
                }
            )
        return out

    def version_file_path(self, record: VersionRecord) -> Path:
        """Return the absolute, confined path of a version's immutable blob."""
        return confine_path(self.settings.data_dir, record.path)

    async def read_version_bytes(self, file_id: str, version: int) -> bytes:
        """Return a version's bytes, verifying the stored hash."""
        rec = await self.get_version(file_id, version)
        if rec is None:
            raise NotFound("file version not found")
        data = self.version_file_path(rec).read_bytes()
        if sha256_bytes(data) != rec.sha256:
            raise Conflict("stored version failed its integrity check")
        return data

    async def verify_claim(self, file_id: str, version: int, sha256: str) -> bool:
        """Return True when a (file, version, hash) triple exists (output-guard check)."""
        rec = await self.get_version(file_id, version)
        return rec is not None and rec.sha256 == sha256
