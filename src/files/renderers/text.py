"""Plain text outputs (txt, md, csv, json, code): bytes are the model's text, scanned and capped."""

from __future__ import annotations

import json

from src.core.errors import ValidationFailure
from src.files.types import TEXT_MIME_BY_EXT, extension_of


def render_text(content: str, filename: str) -> bytes:
    """Encode ``content`` as UTF-8 for a text-family ``filename``.

    JSON is validated and pretty-printed. Other types are written as given with a final newline.
    """
    ext = extension_of(filename)
    if ext not in TEXT_MIME_BY_EXT:
        raise ValidationFailure(f"'{ext}' is not a text output type")
    if "\x00" in content:
        raise ValidationFailure("text content contains a NUL byte")
    if ext == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            raise ValidationFailure(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc
    if not content.endswith("\n"):
        content += "\n"
    return content.encode("utf-8")
