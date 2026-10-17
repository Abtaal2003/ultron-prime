"""Upload validation: size, magic bytes, structure, and bomb checks (spec 11.9 steps 1 to 4).

The extension is a claim, the magic bytes are evidence, and the two must agree. Office files are
ZIP containers, so they are inspected by *actually streaming* every entry through a bounded
decompressor (headers can lie), and every XML part is parsed through ``defusedxml`` before any
real parser sees it. Nothing here executes embedded content.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Final

import defusedxml.common
import defusedxml.ElementTree as SafeET
import filetype
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.config import Settings
from src.core.errors import UltronError
from src.files.types import REFUSED_EXTENSIONS, UPLOAD_TYPES, extension_of

# --- result types ---------------------------------------------------------------------------


class UploadRejected(UltronError):
    """The upload failed validation; ``reason`` is shown to the operator in plain words."""

    kind = "validation"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Detected:
    """A validated upload."""

    ext: str
    family: str
    mime: str
    pages: int | None = None
    width: int | None = None
    height: int | None = None
    warnings: list[str] = field(default_factory=list)


# --- magic bytes ----------------------------------------------------------------------------

_PNG: Final = b"\x89PNG\r\n\x1a\n"


def sniff_family(data: bytes) -> str | None:
    """Identify a supported binary family from magic bytes at offset 0 (or ``None``)."""
    if data.startswith(_PNG):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    return None


_EXT_TO_SNIFF: Final = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
    ".pdf": "pdf",
    ".docx": "zip",
    ".xlsx": "zip",
    ".pptx": "zip",
}
_OFFICE_MARKER: Final = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}


# --- ZIP / Office safety --------------------------------------------------------------------

MAX_ZIP_ENTRIES: Final = 5000
MAX_XML_PART_BYTES: Final = 20 * 1024 * 1024


def inspect_zip(data: bytes, settings: Settings) -> zipfile.ZipFile:
    """Validate a ZIP container and return it open, or raise ``UploadRejected``.

    Streams every entry through the decompressor with running totals, so a header that lies
    about sizes cannot hide a bomb. Also rejects encryption, path traversal names, and nested
    archives.
    """
    max_total = settings.upload_zip_max_uncompressed_mb * 1024 * 1024
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejected("the file is not a valid ZIP-based Office document") from exc
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UploadRejected("the archive structure has too many entries")
    compressed_total = max(len(data), 1)
    declared = sum(i.file_size for i in infos)
    if declared > max_total:
        raise UploadRejected("the document expands to more than the allowed size (zip bomb guard)")
    actual_total = 0
    for info in infos:
        name = info.filename
        if (
            name.startswith(("/", "\\"))
            or ".." in name.replace("\\", "/").split("/")
            or ":" in name[:3]
        ):
            raise UploadRejected("the archive contains unsafe entry names")
        if info.flag_bits & 0x1:
            raise UploadRejected("encrypted archive entries are not accepted")
        if name.lower().endswith((".zip", ".7z", ".rar", ".tar", ".gz", ".exe", ".dll", ".jar")):
            raise UploadRejected(
                "nested archives or executables inside the document are not accepted"
            )
        if info.is_dir():
            continue
        entry_actual = 0
        try:
            with zf.open(info) as stream:
                while chunk := stream.read(1024 * 1024):
                    entry_actual += len(chunk)
                    actual_total += len(chunk)
                    if actual_total > max_total:
                        raise UploadRejected(
                            "the document expands to more than the allowed size (zip bomb guard)"
                        )
                    ratio = entry_actual / max(info.compress_size, 1)
                    if entry_actual > 5 * 1024 * 1024 and ratio > settings.upload_zip_max_ratio:
                        raise UploadRejected(
                            "a part of the document has an extreme compression ratio"
                        )
        except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
            raise UploadRejected(
                "the document is corrupt or uses an unsupported compression"
            ) from exc
    if (
        actual_total / compressed_total > settings.upload_zip_max_ratio
        and actual_total > 1024 * 1024
    ):
        raise UploadRejected(
            f"the document's compression ratio exceeds {settings.upload_zip_max_ratio}:1 "
            "(zip bomb guard)"
        )
    return zf


def check_office_xml(zf: zipfile.ZipFile) -> None:
    """Parse every XML part through ``defusedxml`` (DTDs, entities, and external refs rejected)."""
    for info in zf.infolist():
        lower = info.filename.lower()
        if not lower.endswith((".xml", ".rels", ".vml")):
            continue
        if info.file_size > MAX_XML_PART_BYTES:
            raise UploadRejected("a document part is unreasonably large")
        raw = zf.read(info)
        try:
            SafeET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except defusedxml.common.DefusedXmlException as exc:
            raise UploadRejected(
                "the document uses XML entities or external references (XXE guard)"
            ) from exc
        except SafeET.ParseError as exc:
            raise UploadRejected("the document contains malformed XML") from exc


# --- images and PDFs ------------------------------------------------------------------------


def check_image(data: bytes, ext: str, settings: Settings) -> tuple[int, int]:
    """Validate an image's header and pixel count *before* decoding; return (width, height)."""
    Image.MAX_IMAGE_PIXELS = settings.image_max_pixels
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            fmt = (img.format or "").upper()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise UploadRejected("the image could not be read or is a decompression bomb") from exc
    expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}[ext]
    if fmt != expected:
        raise UploadRejected("the image content does not match its extension")
    if width <= 0 or height <= 0 or width * height > settings.image_max_pixels:
        raise UploadRejected(
            f"the image is larger than the {settings.image_max_pixels:,} pixel limit"
        )
    return width, height


_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|OpenAction|Launch|EmbeddedFile|RichMedia|XFA)\b")


def check_pdf(data: bytes, settings: Settings) -> tuple[int, list[str]]:
    """Validate a PDF: page cap, no encryption, no polyglot tail; report active content."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        raise UploadRejected("the file is both a PDF and a ZIP archive (polyglot)")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise UploadRejected("encrypted PDFs are not accepted")
        pages = len(reader.pages)
    except UploadRejected:
        raise
    except (PdfReadError, ValueError, KeyError, RecursionError, OSError) as exc:
        raise UploadRejected("the PDF could not be read") from exc
    if pages > settings.upload_max_pdf_pages:
        raise UploadRejected(
            f"the PDF has {pages} pages; the limit is {settings.upload_max_pdf_pages}"
        )
    warnings = []
    if _PDF_ACTIVE.search(data):
        warnings.append("pdf_active_content_ignored")
    return pages, warnings


# --- text -----------------------------------------------------------------------------------


def check_text(data: bytes) -> str:
    """Validate that ``data`` is UTF-8 text with no NUL bytes; return the decoded text."""
    if b"\x00" in data[:65536]:
        raise UploadRejected("the file contains binary data but claims to be text")
    if filetype.guess(data[:4096]) is not None:
        raise UploadRejected(
            "the file's content is a binary format that does not match its extension"
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadRejected("text files must be UTF-8") from exc


# --- top level ------------------------------------------------------------------------------


def classify_extension(filename: str) -> tuple[str, str]:
    """Return ``(normalised_ext, family)`` or raise ``UploadRejected`` with a plain reason."""
    ext = extension_of(filename)
    if ext in REFUSED_EXTENSIONS:
        raise UploadRejected(REFUSED_EXTENSIONS[ext])
    info = UPLOAD_TYPES.get(ext)
    if info is None:
        raise UploadRejected(f"'{ext or 'no extension'}' is not an accepted file type")
    return ext, info.family


def detect_and_validate(data: bytes, filename: str, settings: Settings) -> Detected:
    """Run size, type, structure, and bomb checks on one upload."""
    if not data:
        raise UploadRejected("the file is empty")
    if len(data) > settings.upload_max_mb * 1024 * 1024:
        raise UploadRejected(f"the file is larger than {settings.upload_max_mb} MB")
    ext, family = classify_extension(filename)
    info = UPLOAD_TYPES[ext]
    warnings: list[str] = []
    pages = width = height = None
    if family == "text":
        check_text(data)
    else:
        sniffed = sniff_family(data)
        if sniffed is None:
            raise UploadRejected("the file's content does not match its extension")
        if sniffed != _EXT_TO_SNIFF[ext]:
            raise UploadRejected(f"the file is really a {sniffed.upper()}, not a {ext} file")
        if family == "image":
            width, height = check_image(data, ext, settings)
        elif family == "pdf":
            pages, warnings = check_pdf(data, settings)
        else:  # docx, xlsx, pptx
            zf = inspect_zip(data, settings)
            names = {i.filename for i in zf.infolist()}
            if "[Content_Types].xml" not in names or not any(
                n.startswith(_OFFICE_MARKER[ext]) for n in names
            ):
                raise UploadRejected(f"the file is a ZIP but not a valid {ext} document")
            if any(n.lower().endswith(("vbaproject.bin", "vbadata.xml")) for n in names):
                raise UploadRejected("macros are not accepted")
            check_office_xml(zf)
    return Detected(
        ext=".jpg" if ext == ".jpeg" else ext,
        family=family,
        mime=info.mime,
        pages=pages,
        width=width,
        height=height,
        warnings=warnings,
    )


def sniff_matches_extension(data: bytes, ext: str) -> bool:
    """Cheap agreement check used when harvesting sandbox outputs."""
    ext = ext.lower()
    if ext in {".svg", ".html", ".css"} or ext in {
        e for e, i in UPLOAD_TYPES.items() if i.family == "text"
    }:
        try:
            check_text(data)
        except UploadRejected:
            return False
        return True
    expected = _EXT_TO_SNIFF.get(ext)
    return expected is not None and sniff_family(data) == expected
