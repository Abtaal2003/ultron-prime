"""Presentations: slide text, tables, speaker notes, hidden slides, and alt text."""

from __future__ import annotations

import io
from typing import Any

from pptx import Presentation

from src.files.parsers import ParsedDoc, split_chunks


def _shape_lines(shape: Any, lines: list[str], alts: list[str]) -> None:
    descr = getattr(getattr(shape, "_element", None), "xpath", None)
    if descr is not None:
        for node in shape._element.xpath(".//p:cNvPr"):
            alt = node.get("descr")
            if alt and alt.strip():
                alts.append(alt.strip())
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        text = shape.text_frame.text.strip()
        if text:
            lines.append(text)
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            cells = [cell.text.replace("\n", " ") for cell in row.cells]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
    if getattr(shape, "shape_type", None) == 6:  # group
        for inner in shape.shapes:
            _shape_lines(inner, lines, alts)


def parse_pptx(data: bytes) -> ParsedDoc:
    """Extract all slide text, notes, and alt text."""
    prs = Presentation(io.BytesIO(data))
    doc = ParsedDoc(family="pptx", pages=len(prs.slides))
    for index, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        alts: list[str] = []
        for shape in slide.shapes:
            _shape_lines(shape, lines, alts)
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        doc.outline.append(f"{index}. {title or '(untitled)'}")
        hidden = slide._element.get("show") == "0"
        label = "hidden slide" if hidden else ""
        if hidden:
            doc.warnings.append(f"slide {index} is hidden")
        doc.chunks.extend(split_chunks("\n".join(lines), index, label))
        if alts:
            doc.chunks.extend(split_chunks("\n".join(dict.fromkeys(alts)), index, "alt text"))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                doc.chunks.extend(split_chunks(notes.text, index, "speaker notes"))
    return doc
