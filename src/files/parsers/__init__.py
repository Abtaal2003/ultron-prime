"""Parsers: turn validated uploads into text chunks with page and character spans (spec 11.9).

Every text layer is extracted, including hidden, off-page, commented, and speaker-note text, so
the guards see exactly what a model would see. Nothing here executes embedded content: formulas
stay strings, macros are never loaded, PDF actions are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_TOTAL_CHARS = 3_000_000
CHUNK_CHARS = 3000


@dataclass(slots=True)
class ParsedChunk:
    """A span of extracted text. ``page`` is a page, slide, sheet, or pseudo-page number."""

    page: int | None
    start: int
    end: int
    text: str
    label: str = ""

    @property
    def tagged_text(self) -> str:
        """The chunk text prefixed with its label, e.g. ``[comment] ...``."""
        return f"[{self.label}] {self.text}" if self.label else self.text


@dataclass(slots=True)
class ParsedDoc:
    """The result of parsing one file."""

    family: str
    chunks: list[ParsedChunk] = field(default_factory=list)
    outline: list[str] = field(default_factory=list)
    pages: int | None = None
    meta: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        """Total extracted characters."""
        return sum(len(c.text) for c in self.chunks)

    def full_text(self) -> str:
        """All chunk text joined, labels included (used for secret scanning)."""
        return "\n".join(c.tagged_text for c in self.chunks)


def split_chunks(
    text: str, page: int | None, label: str = "", max_chars: int = CHUNK_CHARS
) -> list[ParsedChunk]:
    """Split ``text`` into chunks of at most ``max_chars`` on line boundaries.

    ``start`` and ``end`` are character offsets into ``text`` (the page's text).
    """
    if not text.strip():
        return []
    chunks: list[ParsedChunk] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            cut = text.rfind("\n", start, end)
            if cut > start + max_chars // 2:
                end = cut + 1
            else:
                space = text.rfind(" ", start, end)
                if space > start + max_chars // 2:
                    end = space + 1
        piece = text[start:end]
        if piece.strip():
            chunks.append(ParsedChunk(page=page, start=start, end=end, text=piece, label=label))
        start = end
    return chunks


def parse_attachment(data: bytes, family: str, ext: str) -> ParsedDoc:
    """Dispatch to the parser for ``family`` (a :func:`sniff_family` result)."""
    from src.files.parsers.docx import parse_docx
    from src.files.parsers.image import parse_image
    from src.files.parsers.pdf import parse_pdf
    from src.files.parsers.pptx import parse_pptx
    from src.files.parsers.text import parse_text
    from src.files.parsers.xlsx import parse_xlsx

    if family == "pdf":
        return parse_pdf(data)
    if family == "docx":
        return parse_docx(data)
    if family == "xlsx":
        return parse_xlsx(data)
    if family == "pptx":
        return parse_pptx(data)
    if family == "image":
        return parse_image(data)
    return parse_text(data, ext)
