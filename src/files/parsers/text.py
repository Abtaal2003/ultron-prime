"""Plain text, Markdown, CSV, JSON, YAML, source code, and HTML (as text, never rendered)."""

from __future__ import annotations

import re

from src.files.parsers import CHUNK_CHARS, ParsedDoc, split_chunks

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_PY_DEF = re.compile(r"^(?:async\s+def|def|class)\s+\w+[^\n]*", re.M)


def parse_text(data: bytes, ext: str = ".txt") -> ParsedDoc:
    """Decode UTF-8 text and split it into pseudo-pages of about 3000 characters."""
    text = data.decode("utf-8-sig", errors="replace")
    doc = ParsedDoc(family="text")
    page = 1
    offset = 0
    for chunk in split_chunks(text, page=None, max_chars=CHUNK_CHARS):
        # Pseudo-pages: each chunk is one page; spans are offsets in the whole file.
        doc.chunks.append(
            type(chunk)(
                page=page,
                start=offset + chunk.start,
                end=offset + chunk.end,
                text=chunk.text,
                label="",
            )
        )
        page += 1
    doc.pages = max(page - 1, 1)
    if ext == ".md":
        doc.outline = [
            f"{'  ' * (len(m.group(1)) - 1)}{m.group(2)}" for m in _MD_HEADING.finditer(text)
        ][:80]
    elif ext == ".py":
        doc.outline = [m.group(0).strip() for m in _PY_DEF.finditer(text)][:80]
    doc.meta = {"characters": len(text), "lines": text.count("\n") + 1}
    return doc
