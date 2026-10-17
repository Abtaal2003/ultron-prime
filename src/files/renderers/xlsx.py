"""Spreadsheet and CSV rendering and editing (spec 11.10)."""

from __future__ import annotations

import csv
import io
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.core.errors import NotFound, ValidationFailure
from src.files.specs import (
    AddSheet,
    AppendRows,
    Cell,
    Column,
    FormulaCell,
    RenameSheet,
    SetCells,
    Sheet,
    SheetOp,
    SheetSpec,
)

_FORMATS = {
    "text": "@",
    "integer": "#,##0",
    "number": "#,##0.00",
    "percent": "0.0%",
    "currency": "$#,##0.00",
    "date": "yyyy-mm-dd",
}
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MAX_CELLS = 500_000


def neutralise(text: str) -> str:
    """Prefix a leading formula trigger so CSV viewers treat the cell as text."""
    return "'" + text if text.startswith(_DANGEROUS_PREFIXES) else text


def _write_cell(ws: Any, row: int, col: int, value: Cell, column: Column | None = None) -> None:
    cell = ws.cell(row=row, column=col)
    if isinstance(value, FormulaCell):
        cell.value = value.formula
        cell.data_type = "f"
    elif isinstance(value, str):
        cell.value = value
        cell.data_type = "s"  # never let openpyxl reinterpret "=..." text as a formula
        cell.quotePrefix = value.startswith(_DANGEROUS_PREFIXES)
    else:
        cell.value = value
    if column is not None and not isinstance(value, str):
        cell.number_format = column.number_format or _FORMATS.get(column.type, "General")


def _build_sheet(ws: Any, sheet: Sheet) -> None:
    ws.title = sheet.name
    header_fill = PatternFill("solid", fgColor="1F2937")
    for ci, col in enumerate(sheet.columns, start=1):
        c = ws.cell(row=1, column=ci, value=col.name)
        c.data_type = "s"
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(ci)].width = col.width or max(
            10, min(len(col.name) + 4, 40)
        )
    for ri, row in enumerate(sheet.rows, start=2):
        for ci, value in enumerate(row[: len(sheet.columns)], start=1):
            _write_cell(ws, ri, ci, value, sheet.columns[ci - 1])
    if sheet.freeze_header:
        ws.freeze_panes = "A2"


def _check_size(spec: SheetSpec) -> None:
    total = sum(len(s.rows) * len(s.columns) for s in spec.sheets)
    if total > MAX_CELLS:
        raise ValidationFailure(f"workbook too large ({total} cells; limit {MAX_CELLS})")
    for s in spec.sheets:
        for row in s.rows:
            if len(row) > len(s.columns):
                raise ValidationFailure(f"sheet '{s.name}' has a row wider than its columns")
    names = [s.name.lower() for s in spec.sheets]
    if len(set(names)) != len(names):
        raise ValidationFailure("sheet names must be unique")


def render_xlsx(spec: SheetSpec) -> bytes:
    """Build a workbook from ``spec``."""
    _check_size(spec)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet in spec.sheets:
        _build_sheet(wb.create_sheet(), sheet)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def render_csv(spec: SheetSpec) -> bytes:
    """Render the first sheet as CSV, neutralising formula triggers (formulas stay as text)."""
    _check_size(spec)
    sheet = spec.sheets[0]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([neutralise(c.name) for c in sheet.columns])
    for row in sheet.rows:
        out: list[Any] = []
        for value in row:
            if isinstance(value, FormulaCell):
                out.append(neutralise(value.formula))
            elif isinstance(value, str):
                out.append(neutralise(value))
            elif value is None:
                out.append("")
            else:
                out.append(value)
        writer.writerow(out)
    return buffer.getvalue().encode("utf-8")


def _split_ref(ref: str) -> tuple[int, int]:
    from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

    letters, row = coordinate_from_string(ref)
    return row, column_index_from_string(letters)


def edit_xlsx(data: bytes, ops: list[SheetOp]) -> bytes:
    """Apply ``ops`` to an existing workbook and return the new bytes.

    openpyxl does not preserve charts or images present in the source, so those are dropped.
    """
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    for op in ops:
        if isinstance(op, SetCells):
            ws = _sheet(wb, op.sheet)
            for ref, value in op.cells.items():
                row, col = _split_ref(ref)
                _write_cell(ws, row, col, value)
        elif isinstance(op, AppendRows):
            ws = _sheet(wb, op.sheet)
            start = ws.max_row + 1
            for offset, values in enumerate(op.rows):
                for ci, value in enumerate(values, start=1):
                    _write_cell(ws, start + offset, ci, value)
        elif isinstance(op, AddSheet):
            if op.sheet.name in wb.sheetnames:
                raise ValidationFailure(f"sheet '{op.sheet.name}' already exists")
            _check_size(SheetSpec(sheets=[op.sheet]))
            _build_sheet(wb.create_sheet(), op.sheet)
        elif isinstance(op, RenameSheet):
            ws = _sheet(wb, op.from_name)
            if op.to_name in wb.sheetnames:
                raise ValidationFailure(f"sheet '{op.to_name}' already exists")
            ws.title = op.to_name
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _sheet(wb: Any, name: str) -> Any:
    if name not in wb.sheetnames:
        raise NotFound(f"no sheet named '{name}'; sheets: {wb.sheetnames}")
    return wb[name]
