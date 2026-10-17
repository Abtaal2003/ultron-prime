"""PDF rendering with reportlab (no JavaScript, forms, or actions)."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Flowable,
    Image,
    ListFlowable,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import Paragraph as RLParagraph

from src.core.errors import NotFound
from src.files.renderers.docx import clean
from src.files.specs import Bullets, CodeBlock, DocSpec, ImageBlock, Paragraph
from src.files.specs import Table as TableBlock


def _p(text: str, style: Any) -> RLParagraph:
    return RLParagraph(escape(clean(text)).replace("\n", "<br/>"), style)


def render_pdf(spec: DocSpec, images: Mapping[str, bytes] | None = None) -> bytes:
    """Build a PDF from ``spec``."""
    images = images or {}
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    code_style = ParagraphStyle("code", parent=styles["Code"], fontSize=8, leading=10)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=LETTER,
        title=clean(spec.title)[:200],
        author="Ultron Prime",
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
    )
    flow: list[Flowable] = [_p(spec.title, styles["Title"])]
    if spec.subtitle:
        flow.append(_p(spec.subtitle, styles["Heading3"]))
    for section in spec.sections:
        if section.heading:
            flow.append(_p(section.heading, styles[f"Heading{section.level}"]))
        for block in section.blocks:
            if isinstance(block, Paragraph):
                flow.append(_p(block.text, body))
            elif isinstance(block, Bullets):
                flow.append(
                    ListFlowable(
                        [_p(i, body) for i in block.items],
                        bulletType="1" if block.ordered else "bullet",
                    )
                )
            elif isinstance(block, TableBlock):
                width = len(block.header)
                rows = [[_p(h, body) for h in block.header]] + [
                    [_p(r[i] if i < len(r) else "", body) for i in range(width)] for r in block.rows
                ]
                table = Table(rows, repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ]
                    )
                )
                flow.append(table)
            elif isinstance(block, CodeBlock):
                flow.append(Preformatted(clean(block.text), code_style))
            elif isinstance(block, ImageBlock):
                data = images.get(block.file_id)
                if data is None:
                    raise NotFound(f"image {block.file_id} was not provided")
                img = Image(io.BytesIO(data))
                ratio = img.imageHeight / max(img.imageWidth, 1)
                img.drawWidth = block.width_inches * inch
                img.drawHeight = img.drawWidth * ratio
                flow.append(img)
                if block.caption:
                    flow.append(_p(block.caption, styles["Italic"]))
            flow.append(Spacer(1, 6))
    doc.build(flow)
    return out.getvalue()
