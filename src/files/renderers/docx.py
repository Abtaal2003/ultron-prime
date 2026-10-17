"""Word document rendering and editing with python-docx."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

from docx import Document
from docx.document import Document as DocumentType
from docx.shared import Inches, Pt

from src.core.errors import NotFound, ValidationFailure
from src.files.specs import (
    AppendParagraph,
    Bullets,
    CodeBlock,
    DocOp,
    DocSpec,
    ImageBlock,
    InsertSectionAfter,
    Paragraph,
    ReplaceText,
    Section,
    Table,
    UpdateTableCell,
)

# XML 1.0 forbids most control characters; python-docx raises on them.
_BAD = {c for c in range(32) if c not in (9, 10, 13)}


def clean(text: str) -> str:
    """Drop characters that cannot appear in OOXML text."""
    return "".join(ch for ch in text if ord(ch) not in _BAD)


def _add_section(doc: DocumentType, section: Section, images: Mapping[str, bytes]) -> None:
    if section.heading:
        doc.add_heading(clean(section.heading), level=section.level)
    for block in section.blocks:
        if isinstance(block, Paragraph):
            doc.add_paragraph(clean(block.text))
        elif isinstance(block, Bullets):
            style = "List Number" if block.ordered else "List Bullet"
            for item in block.items:
                doc.add_paragraph(clean(item), style=style)
        elif isinstance(block, Table):
            width = len(block.header)
            table = doc.add_table(rows=1, cols=width)
            table.style = "Table Grid"
            for i, name in enumerate(block.header):
                cell = table.rows[0].cells[i]
                cell.text = clean(name)
                for run in cell.paragraphs[0].runs:
                    run.bold = True
            for row in block.rows:
                cells = table.add_row().cells
                for i in range(width):
                    cells[i].text = clean(row[i]) if i < len(row) else ""
        elif isinstance(block, CodeBlock):
            para = doc.add_paragraph()
            run = para.add_run(clean(block.text))
            run.font.name = "Courier New"
            run.font.size = Pt(9)
        elif isinstance(block, ImageBlock):
            data = images.get(block.file_id)
            if data is None:
                raise NotFound(f"image {block.file_id} was not provided")
            doc.add_picture(io.BytesIO(data), width=Inches(block.width_inches))
            if block.caption:
                doc.add_paragraph().add_run(clean(block.caption)).italic = True


def render_docx(spec: DocSpec, images: Mapping[str, bytes] | None = None) -> bytes:
    """Build a .docx from ``spec`` (core properties are neutral)."""
    images = images or {}
    doc = Document()
    doc.core_properties.author = "Ultron Prime"
    doc.core_properties.title = clean(spec.title)[:200]
    doc.add_heading(clean(spec.title), level=0)
    if spec.subtitle:
        doc.add_paragraph(clean(spec.subtitle), style="Subtitle")
    for section in spec.sections:
        _add_section(doc, section, images)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# --- editing --------------------------------------------------------------------------------


def _paragraphs(doc: DocumentType) -> list[Any]:
    found = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                found.extend(cell.paragraphs)
    return found


def _replace_in_paragraph(par: Any, find: str, replace: str, limit: int | None) -> int:
    if find not in par.text:
        return 0
    count = 0
    for run in par.runs:
        while find in run.text and (limit is None or count < limit):
            run.text = run.text.replace(find, replace, 1)
            count += 1
    remaining = None if limit is None else limit - count
    if find in par.text and (remaining is None or remaining > 0) and par.runs:
        # The match spans runs: merge the paragraph into its first run (formatting of the
        # first run wins).
        merged = par.text
        n = merged.count(find) if remaining is None else min(merged.count(find), remaining)
        merged = merged.replace(find, replace, n)
        par.runs[0].text = merged
        for run in par.runs[1:]:
            run.text = ""
        count += n
    return count


def _heading_level(par: Any) -> int | None:
    name = par.style.name if par.style is not None else ""
    if name == "Title":
        return 0
    if name.startswith("Heading "):
        try:
            return int(name.split()[1])
        except ValueError:
            return None
    return None


def edit_docx(data: bytes, ops: list[DocOp], images: Mapping[str, bytes] | None = None) -> bytes:
    """Apply ``ops`` to an existing .docx and return the new bytes."""
    images = images or {}
    doc = Document(io.BytesIO(data))
    for op in ops:
        if isinstance(op, ReplaceText):
            total = 0
            for par in _paragraphs(doc):
                left = None if op.all else 1 - total
                if left is not None and left <= 0:
                    break
                total += _replace_in_paragraph(par, clean(op.find), clean(op.replace), left)
            if total == 0:
                raise NotFound(f"text '{op.find[:60]}' was not found")
        elif isinstance(op, AppendParagraph):
            doc.add_paragraph(clean(op.text), style=op.style)
        elif isinstance(op, UpdateTableCell):
            if op.table_index >= len(doc.tables):
                raise NotFound(f"table {op.table_index} does not exist ({len(doc.tables)} tables)")
            table = doc.tables[op.table_index]
            if op.row >= len(table.rows) or op.col >= len(table.columns):
                raise NotFound("cell is outside the table")
            table.cell(op.row, op.col).text = clean(op.text)
        elif isinstance(op, InsertSectionAfter):
            _insert_section_after(doc, op, images)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _insert_section_after(
    doc: DocumentType, op: InsertSectionAfter, images: Mapping[str, bytes]
) -> None:
    body = doc.element.body
    pars = doc.paragraphs
    anchor_index = next(
        (
            i
            for i, p in enumerate(pars)
            if _heading_level(p) is not None and p.text.strip() == op.after_heading.strip()
        ),
        None,
    )
    if anchor_index is None:
        raise NotFound(f"no heading named '{op.after_heading[:60]}'")
    level = _heading_level(pars[anchor_index]) or 0
    end_el = pars[anchor_index]._p
    for p in pars[anchor_index + 1 :]:
        lvl = _heading_level(p)
        if lvl is not None and lvl <= level:
            break
        end_el = p._p
    # Tables between paragraphs of the section also belong to it: walk siblings.
    sibling = end_el.getnext()
    while sibling is not None and sibling.tag.endswith("}tbl"):
        end_el = sibling
        sibling = sibling.getnext()
    before = list(body)
    _add_section(doc, op.section, images)
    created = [el for el in body if el not in before and not el.tag.endswith("}sectPr")]
    for el in reversed(created):
        end_el.addnext(el)


def validate_docx_text(spec: DocSpec) -> None:
    """Reject specs that are empty after cleaning."""
    if not clean(spec.title).strip():
        raise ValidationFailure("title is empty")
