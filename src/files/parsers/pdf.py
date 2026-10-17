"""PDF text extraction (every page, including annotations) and on-demand page rendering."""

from __future__ import annotations

import io
from typing import Any

import pypdfium2
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.files.parsers import MAX_TOTAL_CHARS, ParsedChunk, ParsedDoc, split_chunks


def _annotation_text(page: Any) -> str:
    parts: list[str] = []
    annots = page.get("/Annots")
    if not annots:
        return ""
    try:
        for ref in annots:
            annot = ref.get_object()
            for key in ("/Contents", "/V", "/TU", "/T"):
                value = annot.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    except (PdfReadError, ValueError, KeyError, AttributeError, TypeError, RecursionError):
        return "\n".join(parts)
    return "\n".join(dict.fromkeys(parts))


def _flatten_outline(items: Any, depth: int = 0, out: list[str] | None = None) -> list[str]:
    out = out if out is not None else []
    for item in items:
        if len(out) >= 50:
            break
        if isinstance(item, list):
            _flatten_outline(item, depth + 1, out)
        else:
            title = getattr(item, "title", None)
            if title:
                out.append(f"{'  ' * depth}{title}")
    return out


def parse_pdf(data: bytes) -> ParsedDoc:
    """Extract text per page (and annotation text), plus the outline."""
    reader = PdfReader(io.BytesIO(data), strict=False)
    doc = ParsedDoc(family="pdf", pages=len(reader.pages))
    total = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except (PdfReadError, ValueError, KeyError, TypeError, RecursionError, AttributeError):
            doc.warnings.append(f"page {index}: text extraction failed")
            text = ""
        doc.chunks.extend(split_chunks(text, page=index))
        notes = _annotation_text(page)
        if notes:
            doc.chunks.extend(split_chunks(notes, page=index, label="annotation"))
        total += len(text) + len(notes)
        if total > MAX_TOTAL_CHARS:
            doc.warnings.append(f"text truncated after page {index}")
            break
    if not any(c.text.strip() for c in doc.chunks):
        doc.warnings.append("no extractable text (possibly a scan); render pages to read them")
    try:
        doc.outline = _flatten_outline(reader.outline)
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError):
        doc.warnings.append("outline unreadable")
    try:
        info = reader.metadata
        if info and info.title:
            doc.meta["title"] = str(info.title)[:200]
    except (PdfReadError, ValueError, KeyError, TypeError):
        pass
    return doc


def render_pdf_page(data: bytes, page_number: int, max_pixels: int = 12_000_000) -> bytes:
    """Render one page (1-based) to PNG with PDFium. No JavaScript is executed."""
    pdf = pypdfium2.PdfDocument(data)
    try:
        if not 1 <= page_number <= len(pdf):
            raise ValueError("page out of range")
        page = pdf[page_number - 1]
        width, height = page.get_size()
        scale = 1.5
        if width * height * scale * scale > max_pixels:
            scale = (max_pixels / max(width * height, 1)) ** 0.5
        image = page.render(scale=scale).to_pil().convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        pdf.close()


__all__ = ["ParsedChunk", "parse_pdf", "render_pdf_page"]
