"""The upload pipeline: validate, sanitise, parse, scan, store (spec 11.9).

Order matters. Nothing is stored until the bytes pass type, size, structure, and bomb checks.
Images are re-encoded to drop metadata. Parsing runs in a worker thread with a time limit, and
the extracted text is scanned for credentials before the operator is told the file is ready.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.config import Settings
from src.core.errors import UltronError
from src.files.parsers import ParsedDoc, parse_attachment
from src.files.parsers.image import normalise_image
from src.files.store import AttachmentRecord, FileStore
from src.files.validators import UploadRejected, detect_and_validate
from src.logging_setup import get_logger
from src.tools.security import SecretScanner, sanitize_filename

log = get_logger("ingest")

PARSE_TIMEOUT_S = 60.0
EXTERNAL_FAMILIES = frozenset({"pdf", "docx", "xlsx", "pptx"})


@dataclass(slots=True)
class IngestResult:
    """What the operator and the agent learn about one accepted upload."""

    record: AttachmentRecord
    family: str
    outline: list[str]
    warnings: list[str]
    chunk_count: int
    char_count: int
    quarantine: bool
    secret_warning: bool
    meta: dict[str, object] = field(default_factory=dict)


def requires_quarantine(settings: Settings, family: str) -> bool:
    """Whether reads of this file must go through the quarantined reader.

    ``standard``: office and PDF documents (from outside) are quarantined when the
    ``uploads_external`` source is enabled; text and images are the operator's own material and
    stay raw. ``strict`` quarantines everything. ``off`` quarantines nothing.
    """
    if settings.quarantine_mode == "off":
        return False
    if settings.quarantine_mode == "strict":
        return True
    return "uploads_external" in settings.quarantine_sources and family in EXTERNAL_FAMILIES


def check_batch(settings: Settings, sizes: list[int]) -> None:
    """Enforce the per-message file-count and total-size limits."""
    if len(sizes) > settings.upload_max_files_per_message:
        raise UploadRejected(
            f"at most {settings.upload_max_files_per_message} files can be sent in one message"
        )
    if sum(sizes) > settings.upload_max_mb_per_message * 1024 * 1024:
        raise UploadRejected(
            f"the files together exceed {settings.upload_max_mb_per_message} MB for one message"
        )


class FileService:
    """Turns raw uploads into stored, parsed attachments."""

    def __init__(self, store: FileStore, settings: Settings, scanner: SecretScanner) -> None:
        self.store = store
        self.settings = settings
        self.scanner = scanner

    async def ingest(
        self,
        *,
        chat_id: str,
        filename: str,
        data: bytes,
        message_id: str | None = None,
    ) -> IngestResult:
        """Validate and store one upload. Raises :class:`UploadRejected` with a plain reason."""
        safe_name = sanitize_filename(filename)
        detected = await asyncio.to_thread(detect_and_validate, data, safe_name, self.settings)
        stored = data
        mime = detected.mime
        if detected.family == "image" and self.settings.image_strip_metadata:
            stored, mime = await asyncio.to_thread(normalise_image, data)
        parsed = await self._parse(stored, detected.family, detected.ext)
        warnings = list(dict.fromkeys([*detected.warnings, *parsed.warnings]))
        secret = bool(parsed.chunks) and self.scanner.contains_secret(parsed.full_text())
        if secret:
            warnings.append("the file appears to contain credentials; they will not be echoed")
        record = await self.store.save_attachment(
            chat_id=chat_id,
            original_name=filename[:255],
            safe_name=safe_name,
            detected_mime=mime,
            data=stored,
            message_id=message_id,
            pages=parsed.pages or detected.pages,
            width=detected.width,
            height=detected.height,
            secret_warning=secret,
        )
        await self.store.add_chunks(
            record.id, [(c.page, c.start, c.end, c.tagged_text) for c in parsed.chunks]
        )
        log.info("upload_ingested", attachment_id=record.id, family=detected.family)
        return IngestResult(
            record=record,
            family=detected.family,
            outline=parsed.outline,
            warnings=warnings,
            chunk_count=len(parsed.chunks),
            char_count=parsed.total_chars,
            quarantine=requires_quarantine(self.settings, detected.family),
            secret_warning=secret,
            meta=parsed.meta,
        )

    async def _parse(self, data: bytes, family: str, ext: str) -> ParsedDoc:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(parse_attachment, data, family, ext), timeout=PARSE_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise UploadRejected("the file took too long to read") from exc
        except UltronError:
            raise
        except Exception as exc:  # parsers meet hostile input; any failure is a rejection
            log.warning("parse_failed", family=family, error=type(exc).__name__)
            raise UploadRejected(f"the file could not be read ({type(exc).__name__})") from exc
