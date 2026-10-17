"""Spreadsheets: values, formulas (as strings), comments, hidden sheets, defined names.

Workbooks are opened with ``openpyxl`` in read-only mode for the bulk pass. Formulas are never
evaluated and external links are never followed.
"""

from __future__ import annotations

import io
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter

from src.files.parsers import ParsedDoc, split_chunks

MAX_ROWS = 2000
MAX_COLS = 40


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


def parse_xlsx(data: bytes) -> ParsedDoc:
    """Extract every sheet (capped), cell comments, defined names, and hidden-sheet flags."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=False)
    doc = ParsedDoc(family="xlsx", pages=len(wb.worksheets))
    try:
        for index, ws in enumerate(wb.worksheets, start=1):
            hidden = ws.sheet_state != "visible"
            if hidden:
                doc.warnings.append(f"sheet '{ws.title}' is hidden")
            doc.outline.append(f"{ws.title} ({ws.max_row} rows x {ws.max_column} cols)")
            lines: list[str] = []
            comments: list[str] = []
            truncated = ws.max_row > MAX_ROWS or ws.max_column > MAX_COLS
            for row in ws.iter_rows(
                min_row=1, max_row=min(ws.max_row, MAX_ROWS), max_col=min(ws.max_column, MAX_COLS)
            ):
                values = [_cell_text(c.value) for c in row]
                for c in row:
                    if c.comment is not None and c.comment.text:
                        comments.append(f"{c.coordinate}: {c.comment.text.strip()}")
                if any(values):
                    lines.append(f"{row[0].row}\t" + "\t".join(values).rstrip("\t"))
            if truncated:
                doc.warnings.append(
                    f"sheet '{ws.title}' truncated to {MAX_ROWS} rows x {MAX_COLS} columns"
                )
            label = "hidden sheet" if hidden else ""
            header = f"Sheet: {ws.title}"
            doc.chunks.extend(split_chunks(header + "\n" + "\n".join(lines), index, label))
            if comments:
                doc.chunks.extend(split_chunks("\n".join(comments), index, "cell comment"))
        names: list[str] = []
        for name, defined in wb.defined_names.items():
            names.append(f"{name} = {str(defined.attr_text)[:200]}")
        if names:
            doc.chunks.extend(split_chunks("\n".join(names), None, "defined name"))
    finally:
        wb.close()
    return doc


def read_range(data: bytes, sheet: str, cell_range: str, max_cells: int = 4000) -> list[list[str]]:
    """Return the values in an A1 range as strings (formulas stay as text)."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"no sheet named '{sheet}'; sheets: {wb.sheetnames}")
        ws = wb[sheet]
        cells = ws[cell_range]
        if not isinstance(cells, tuple):
            cells = ((cells,),)
        elif cells and not isinstance(cells[0], tuple):
            cells = (cells,)
        rows = [[_cell_text(c.value) for c in row] for row in cells]
        if sum(len(r) for r in rows) > max_cells:
            raise ValueError(f"range too large (limit {max_cells} cells)")
        return rows
    finally:
        wb.close()


def column_letters(n: int) -> list[str]:
    """Return the first ``n`` column letters."""
    return [get_column_letter(i) for i in range(1, n + 1)]
