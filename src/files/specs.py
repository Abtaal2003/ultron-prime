"""Structured specs for ``artifact_forge`` (spec 11.10).

The model never writes raw file bytes. It describes the document, spreadsheet, or chart with these
schemas and a deterministic renderer builds the file, which keeps outputs valid, reviewable, and
free of active content.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator

from src.tools.base import StrictModel

MAX_SECTIONS = 200
MAX_BLOCKS_PER_SECTION = 100
MAX_TABLE_ROWS_IN_DOC = 2000
MAX_TEXT = 20_000

# --- documents ------------------------------------------------------------------------------


class Paragraph(StrictModel):
    """A paragraph of text."""

    kind: Literal["paragraph"] = "paragraph"
    text: str = Field(..., max_length=MAX_TEXT)


class Bullets(StrictModel):
    """A bulleted or numbered list."""

    kind: Literal["bullets"] = "bullets"
    items: list[str] = Field(..., min_length=1, max_length=200)
    ordered: bool = False

    @field_validator("items")
    @classmethod
    def _item_len(cls, items: list[str]) -> list[str]:
        if any(len(i) > 2000 for i in items):
            raise ValueError("list items are limited to 2000 characters")
        return items


class Table(StrictModel):
    """A table with a header row."""

    kind: Literal["table"] = "table"
    header: list[str] = Field(..., min_length=1, max_length=30)
    rows: list[list[str]] = Field(default_factory=list, max_length=MAX_TABLE_ROWS_IN_DOC)

    @field_validator("rows")
    @classmethod
    def _row_shape(cls, rows: list[list[str]]) -> list[list[str]]:
        if any(len(r) > 30 for r in rows):
            raise ValueError("tables are limited to 30 columns")
        return rows


class ImageBlock(StrictModel):
    """An image taken from an attachment or generated file."""

    kind: Literal["image"] = "image"
    file_id: str = Field(..., description="att_... or file_... id of a PNG or JPEG.")
    caption: str | None = Field(None, max_length=300)
    width_inches: float = Field(5.0, ge=1.0, le=6.5)


class CodeBlock(StrictModel):
    """Preformatted code or text."""

    kind: Literal["code"] = "code"
    text: str = Field(..., max_length=MAX_TEXT)
    language: str | None = Field(None, max_length=30)


Block = Annotated[Paragraph | Bullets | Table | ImageBlock | CodeBlock, Field(discriminator="kind")]


class Section(StrictModel):
    """A heading followed by blocks."""

    heading: str | None = Field(None, max_length=200)
    level: int = Field(1, ge=1, le=3)
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS_PER_SECTION)


class DocSpec(StrictModel):
    """A whole document."""

    title: str = Field(..., min_length=1, max_length=200)
    subtitle: str | None = Field(None, max_length=300)
    sections: list[Section] = Field(..., min_length=1, max_length=MAX_SECTIONS)


# --- spreadsheets ---------------------------------------------------------------------------

ALLOWED_FUNCTIONS = frozenset({"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "IF"})
_FORMULA_CHARS = re.compile(r"^=[A-Za-z0-9_ ,.:()+\-*/<>=$!\"'%&]+$")
_FUNCTION_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_SHEET_NAME_BAD = re.compile(r"[\[\]:*?/\\]")
_CELL_REF = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")


def validate_formula(formula: str) -> str:
    """Accept only simple formulas that use allow-listed functions and local references."""
    if not formula.startswith("="):
        raise ValueError("formulas must start with '='")
    if len(formula) > 200:
        raise ValueError("formula too long")
    if not _FORMULA_CHARS.match(formula):
        raise ValueError("formula contains characters that are not allowed")
    lowered = formula.lower()
    if "http" in lowered or "file:" in lowered or "\\\\" in formula:
        raise ValueError("formulas may not reference external resources")
    for name in _FUNCTION_CALL.findall(formula):
        if name.upper() not in ALLOWED_FUNCTIONS:
            raise ValueError(
                f"function {name} is not allowed (allowed: {sorted(ALLOWED_FUNCTIONS)})"
            )
    return formula


class FormulaCell(StrictModel):
    """A live formula (allow-listed functions only)."""

    formula: str = Field(..., max_length=200)

    @field_validator("formula")
    @classmethod
    def _valid(cls, value: str) -> str:
        return validate_formula(value)


Cell = str | int | float | bool | FormulaCell | None


class Column(StrictModel):
    """One column's header and type."""

    name: str = Field(..., min_length=1, max_length=255)
    type: Literal["text", "integer", "number", "percent", "currency", "date"] = "text"
    number_format: str | None = Field(None, max_length=40, pattern=r"^[0#,.\-%$ ()A-Za-z/:]*$")
    width: int | None = Field(None, ge=4, le=80)


class Sheet(StrictModel):
    """One worksheet."""

    name: str = Field(..., min_length=1, max_length=31)
    columns: list[Column] = Field(..., min_length=1, max_length=100)
    rows: list[list[Cell]] = Field(default_factory=list)
    freeze_header: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if _SHEET_NAME_BAD.search(value) or value.strip() != value:
            raise ValueError("sheet names cannot contain [ ] : * ? / \\ or edge spaces")
        return value


class SheetSpec(StrictModel):
    """A workbook (or, for CSV output, its first sheet)."""

    sheets: list[Sheet] = Field(..., min_length=1, max_length=20)


# --- charts ---------------------------------------------------------------------------------


class Series(StrictModel):
    """One data series."""

    name: str = Field("", max_length=80)
    x: list[float | str] | None = Field(None, max_length=5000)
    y: list[float] = Field(..., min_length=1, max_length=5000)


class ChartSpec(StrictModel):
    """A chart."""

    type: Literal["line", "bar", "scatter", "histogram", "pie"]
    title: str = Field("", max_length=200)
    x_label: str = Field("", max_length=100)
    y_label: str = Field("", max_length=100)
    series: list[Series] = Field(..., min_length=1, max_length=12)
    categories: list[str] | None = Field(None, max_length=200)
    bins: int = Field(10, ge=2, le=100)
    legend: bool = True
    width_px: int = Field(1000, ge=300, le=2000)
    height_px: int = Field(600, ge=200, le=1500)


# --- edit operations ------------------------------------------------------------------------


class ReplaceText(StrictModel):
    """Replace text throughout a document."""

    op: Literal["replace_text"] = "replace_text"
    find: str = Field(..., min_length=1, max_length=500)
    replace: str = Field(..., max_length=5000)
    all: bool = True


class InsertSectionAfter(StrictModel):
    """Insert a new section after the named heading's section."""

    op: Literal["insert_section_after"] = "insert_section_after"
    after_heading: str = Field(..., min_length=1, max_length=200)
    section: Section


class UpdateTableCell(StrictModel):
    """Change one table cell."""

    op: Literal["update_table_cell"] = "update_table_cell"
    table_index: int = Field(..., ge=0)
    row: int = Field(..., ge=0)
    col: int = Field(..., ge=0)
    text: str = Field(..., max_length=5000)


class AppendParagraph(StrictModel):
    """Add a paragraph at the end."""

    op: Literal["append_paragraph"] = "append_paragraph"
    text: str = Field(..., max_length=MAX_TEXT)
    style: Literal["Normal", "Heading 1", "Heading 2", "Heading 3", "List Bullet"] = "Normal"


DocOp = Annotated[
    ReplaceText | InsertSectionAfter | UpdateTableCell | AppendParagraph, Field(discriminator="op")
]


class SetCells(StrictModel):
    """Set cells by A1 reference."""

    op: Literal["set_cells"] = "set_cells"
    sheet: str = Field(..., max_length=31)
    cells: dict[str, Cell] = Field(..., min_length=1, max_length=2000)

    @field_validator("cells")
    @classmethod
    def _refs(cls, cells: dict[str, Cell]) -> dict[str, Cell]:
        for ref in cells:
            if not _CELL_REF.match(ref):
                raise ValueError(f"'{ref}' is not an A1 cell reference")
        return cells


class AppendRows(StrictModel):
    """Append rows to a sheet."""

    op: Literal["append_rows"] = "append_rows"
    sheet: str = Field(..., max_length=31)
    rows: list[list[Cell]] = Field(..., min_length=1)


class AddSheet(StrictModel):
    """Add a worksheet."""

    op: Literal["add_sheet"] = "add_sheet"
    sheet: Sheet


class RenameSheet(StrictModel):
    """Rename a worksheet."""

    op: Literal["rename_sheet"] = "rename_sheet"
    from_name: str = Field(..., max_length=31)
    to_name: str = Field(..., min_length=1, max_length=31)

    @field_validator("to_name")
    @classmethod
    def _name(cls, value: str) -> str:
        if _SHEET_NAME_BAD.search(value) or value.strip() != value:
            raise ValueError("sheet names cannot contain [ ] : * ? / \\ or edge spaces")
        return value


SheetOp = Annotated[SetCells | AppendRows | AddSheet | RenameSheet, Field(discriminator="op")]


def spec_strings(node: object) -> list[str]:
    """Collect every string inside a (dumped) spec, for the secret scan."""
    found: list[str] = []
    if isinstance(node, str):
        found.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            found.append(str(key))
            found.extend(spec_strings(value))
    elif isinstance(node, list | tuple):
        for item in node:
            found.extend(spec_strings(item))
    return found
