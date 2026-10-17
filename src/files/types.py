"""File-type tables: what may come in, what may go out (spec 11.9, 11.10)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@dataclass(frozen=True, slots=True)
class TypeInfo:
    """One accepted file type."""

    family: str  # image | pdf | docx | xlsx | pptx | text
    mime: str


UPLOAD_TYPES: dict[str, TypeInfo] = {
    ".png": TypeInfo("image", "image/png"),
    ".jpg": TypeInfo("image", "image/jpeg"),
    ".jpeg": TypeInfo("image", "image/jpeg"),
    ".webp": TypeInfo("image", "image/webp"),
    ".gif": TypeInfo("image", "image/gif"),
    ".pdf": TypeInfo("pdf", "application/pdf"),
    ".docx": TypeInfo("docx", DOCX_MIME),
    ".xlsx": TypeInfo("xlsx", XLSX_MIME),
    ".pptx": TypeInfo("pptx", PPTX_MIME),
    ".txt": TypeInfo("text", "text/plain"),
    ".md": TypeInfo("text", "text/markdown"),
    ".csv": TypeInfo("text", "text/csv"),
    ".tsv": TypeInfo("text", "text/tab-separated-values"),
    ".json": TypeInfo("text", "application/json"),
    ".yaml": TypeInfo("text", "application/yaml"),
    ".yml": TypeInfo("text", "application/yaml"),
    ".toml": TypeInfo("text", "application/toml"),
    ".py": TypeInfo("text", "text/x-python"),
    ".js": TypeInfo("text", "text/javascript"),
    ".ts": TypeInfo("text", "text/typescript"),
    ".sql": TypeInfo("text", "application/sql"),
    ".log": TypeInfo("text", "text/plain"),
    ".html": TypeInfo("text", "text/html"),
}

# Reasons for refusing well-known extensions, shown to the operator in plain words.
REFUSED_EXTENSIONS: dict[str, str] = {
    ".zip": "archives are not accepted",
    ".tar": "archives are not accepted",
    ".gz": "archives are not accepted",
    ".tgz": "archives are not accepted",
    ".7z": "archives are not accepted",
    ".rar": "archives are not accepted",
    ".exe": "executables are not accepted",
    ".dll": "executables are not accepted",
    ".so": "executables are not accepted",
    ".bin": "executables are not accepted",
    ".sh": "executables are not accepted",
    ".bat": "executables are not accepted",
    ".msi": "executables are not accepted",
    ".docm": "macro-enabled Office files are not accepted",
    ".xlsm": "macro-enabled Office files are not accepted",
    ".pptm": "macro-enabled Office files are not accepted",
    ".doc": "legacy binary Office formats are not accepted (save as .docx)",
    ".xls": "legacy binary Office formats are not accepted (save as .xlsx)",
    ".ppt": "legacy binary Office formats are not accepted (save as .pptx)",
    ".svg": "SVG can carry script and is not accepted",
    ".heic": "HEIC photos are not supported yet; share a JPEG or PNG instead",
    ".heif": "HEIC photos are not supported yet; share a JPEG or PNG instead",
    ".mp3": "audio is not accepted",
    ".wav": "audio is not accepted",
    ".m4a": "audio is not accepted",
    ".mp4": "video is not accepted",
    ".mov": "video is not accepted",
    ".avi": "video is not accepted",
    ".mkv": "video is not accepted",
}

TEXT_EXTENSIONS = frozenset(ext for ext, info in UPLOAD_TYPES.items() if info.family == "text")

# Types the scribe may read and write (11.5). Binary formats go through the forge.
SCRIBE_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".json",
        ".csv",
        ".py",
        ".js",
        ".ts",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".log",
        ".html",
        ".css",
    }
)

TEXT_MIME_BY_EXT: dict[str, str] = {
    **{ext: info.mime for ext, info in UPLOAD_TYPES.items() if info.family == "text"},
    ".css": "text/css",
}

# Everything the forge or the sandbox harvest may emit. SVG is download-only (never previewed).
OUTPUT_TYPES: dict[str, str] = {
    **TEXT_MIME_BY_EXT,
    ".docx": DOCX_MIME,
    ".xlsx": XLSX_MIME,
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# Never rendered inline by the app; offered as downloads only.
DOWNLOAD_ONLY_MIMES = frozenset({"image/svg+xml", "text/html"})

# Sandbox harvest: no SVG or HTML (a generated chart is a PNG).
HARVEST_EXTENSIONS = frozenset(OUTPUT_TYPES) - {".svg", ".html"}


def extension_of(name: str) -> str:
    """Return the lower-cased final extension of ``name`` (``""`` if none)."""
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def mime_for_output(name: str) -> str:
    """Return the MIME type for an output file name (``application/octet-stream`` if unknown)."""
    return OUTPUT_TYPES.get(extension_of(name), "application/octet-stream")
