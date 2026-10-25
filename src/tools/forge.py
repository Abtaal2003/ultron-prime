"""Tool: ``artifact_forge`` (spec 11.10). Creates and edits documents, sheets, charts, and text.

The model supplies a structured spec; a deterministic renderer writes the file. Nothing the model
says is ever executed or saved as raw binary. Uploaded files are never modified: an edit of an
attachment writes a new versioned workspace file marked as containing external content.
"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import PurePosixPath
from typing import Any, ClassVar, Literal

import openpyxl
from pydantic import BaseModel, Field, model_validator

from src.core.errors import LimitExceeded, NotFound, PolicyDenied, ValidationFailure
from src.files.parsers import parse_attachment
from src.files.parsers.xlsx import read_range
from src.files.renderers.chart import render_chart
from src.files.renderers.docx import edit_docx, render_docx
from src.files.renderers.pdf import render_pdf
from src.files.renderers.text import render_text
from src.files.renderers.xlsx import edit_xlsx, render_csv, render_xlsx
from src.files.specs import (
    ChartSpec,
    Column,
    DocOp,
    DocSpec,
    ImageBlock,
    Paragraph,
    Section,
    Sheet,
    SheetOp,
    SheetSpec,
    spec_strings,
)
from src.files.types import DOCX_MIME, OUTPUT_TYPES, XLSX_MIME, extension_of
from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput
from src.tools.security import sanitize_filename

Action = Literal[
    "create_document",
    "create_spreadsheet",
    "create_chart",
    "create_text",
    "edit_document",
    "edit_spreadsheet",
    "convert",
]
OUTPUT_LIMIT = 25 * 1024 * 1024
OUTPUT_DIR = "outputs"
_DOC_EXTS = {".docx", ".pdf"}
_SHEET_EXTS = {".xlsx", ".csv"}


class ForgeInput(StrictModel):
    """One file operation. Fill only the fields that belong to ``action``."""

    action: Action
    filename: str | None = Field(
        None,
        max_length=120,
        description="Output file name with extension, e.g. 'report.docx'. Required for create_*.",
    )
    document: DocSpec | None = Field(None, description="For create_document.")
    spreadsheet: SheetSpec | None = Field(None, description="For create_spreadsheet.")
    chart: ChartSpec | None = Field(None, description="For create_chart.")
    content: str | None = Field(None, max_length=200_000, description="For create_text.")
    source: str | None = Field(
        None,
        max_length=200,
        description="For edit_* and convert: an attachment id (att_...), file id (file_...), or "
        "workspace file name.",
    )
    doc_ops: list[DocOp] | None = Field(None, max_length=50, description="For edit_document.")
    sheet_ops: list[SheetOp] | None = Field(
        None, max_length=50, description="For edit_spreadsheet."
    )
    convert_to: Literal["pdf", "csv", "xlsx"] | None = Field(None, description="For convert.")
    sheet: str | None = Field(None, max_length=31, description="Sheet to export when converting.")

    @model_validator(mode="after")
    def _check(self) -> ForgeInput:
        need: dict[str, list[str]] = {
            "create_document": ["filename", "document"],
            "create_spreadsheet": ["filename", "spreadsheet"],
            "create_chart": ["filename", "chart"],
            "create_text": ["filename", "content"],
            "edit_document": ["source", "doc_ops"],
            "edit_spreadsheet": ["source", "sheet_ops"],
            "convert": ["source", "convert_to"],
        }
        missing = [f for f in need[self.action] if getattr(self, f) in (None, [])]
        if missing:
            raise ValueError(f"{self.action} requires: {', '.join(missing)}")
        return self


class ForgeOutput(BaseModel):
    """The created or changed file."""

    action: str
    file_id: str
    path: str
    version: int
    size: int
    mime: str
    sha256: str
    external_content: bool
    note: str = ""


class ArtifactForge(Tool):
    """Create or edit documents, spreadsheets, charts, and text files."""

    name: ClassVar[str] = "artifact_forge"
    description: ClassVar[str] = (
        "Create or edit files from a structured description: Word/PDF documents, Excel/CSV "
        "spreadsheets (simple formulas SUM, AVERAGE, MIN, MAX, COUNT, IF), PNG charts, and "
        "text/markdown/JSON/code files. You never write raw file bytes. Edits to an uploaded "
        "file create a new copy in the workspace and leave the upload untouched. Use "
        "file_system_scribe for small text edits and the sandbox for computed data. "
        "Fill only the field named for the action. Examples: "
        '{"action": "create_text", "filename": "notes.md", "content": "# Notes"}; '
        '{"action": "create_document", "filename": "status.docx", "document": {"title": '
        '"Status", "sections": [{"heading": "Done", "blocks": [{"kind": "paragraph", '
        '"text": "Parser shipped."}]}]}}; '
        '{"action": "create_spreadsheet", "filename": "costs.xlsx", "spreadsheet": {"sheets": '
        '[{"name": "Costs", "columns": [{"name": "item"}, {"name": "eur", "type": "number"}], '
        '"rows": [["coffee", 12.5]]}]}}; '
        '{"action": "convert", "source": "costs.xlsx", "convert_to": "csv"}.'
    )
    input_model: ClassVar[type[StrictModel]] = ForgeInput
    output_model: ClassVar[type[BaseModel]] = ForgeOutput
    timeout_s: ClassVar[float] = 60.0
    start_line: ClassVar[str] = "forge_start"
    done_line: ClassVar[str] = "forge_done"

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Dispatch on the action, scan the result, and store a new version."""
        inp: ForgeInput = args
        if ctx.services is None:
            raise ValidationFailure("services are not available")
        self._scan_inputs(inp, ctx)
        derived: list[str] = []
        external = False
        note = ""
        if inp.action.startswith("create_"):
            name, data = await self._create(inp, ctx)
            logical = f"{OUTPUT_DIR}/{name}"
            change = "created"
        else:
            assert inp.source is not None
            src_bytes, src_name, derived, external = await self._load_source(inp.source, ctx)
            name, data, note = await self._transform(inp, ctx, src_bytes, src_name)
            logical = (
                f"{OUTPUT_DIR}/{name}"
                if inp.source.startswith("att_") or inp.action == "convert"
                else src_name
            )
            change = f"{inp.action} from {inp.source}"
        if len(data) > OUTPUT_LIMIT:
            raise LimitExceeded(f"output exceeds the {OUTPUT_LIMIT // (1024 * 1024)} MB limit")
        if extension_of(name) not in OUTPUT_TYPES:
            raise ValidationFailure(
                f"'{extension_of(name) or 'no extension'}' is not an allowed output type"
            )
        record = await ctx.services.files.write_version(
            logical_name=logical,
            data=data,
            mime=OUTPUT_TYPES[extension_of(name)],
            created_by="forge",
            chat_id=ctx.chat_id,
            kind="generated",
            derived_from=derived,
            sources=["operator"] if not ctx.tainted else ["operator", "untrusted_turn"],
            external_content=external or ctx.tainted,
            change_note=change,
            max_bytes=OUTPUT_LIMIT,
        )
        out = ForgeOutput(
            action=inp.action,
            file_id=record.file_id,
            path=record.path,
            version=record.version,
            size=record.size_bytes,
            mime=record.mime,
            sha256=record.sha256,
            external_content=record.external_content,
            note=note,
        )
        return ToolOutput(data=out, files=[record.to_ref()], taints_turn=False)

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _scan_inputs(inp: ForgeInput, ctx: ToolContext) -> None:
        """Refuse to write credential-shaped text into a file."""
        assert ctx.services is not None
        payload = inp.model_dump(mode="json", exclude_none=True)
        texts = spec_strings(payload)
        if any(ctx.services.scanner.contains_secret(t) for t in texts):
            raise PolicyDenied("the content looks like it contains a credential; not writing it")

    async def _create(self, inp: ForgeInput, ctx: ToolContext) -> tuple[str, bytes]:
        name = sanitize_filename(inp.filename or "", fallback="output")
        ext = extension_of(name)
        if inp.action == "create_document":
            assert inp.document is not None
            if ext not in _DOC_EXTS:
                raise ValidationFailure("documents must be .docx or .pdf")
            images = await self._images(inp.document, ctx)
            renderer = render_docx if ext == ".docx" else render_pdf
            return name, await asyncio.to_thread(renderer, inp.document, images)
        if inp.action == "create_spreadsheet":
            assert inp.spreadsheet is not None
            if ext not in _SHEET_EXTS:
                raise ValidationFailure("spreadsheets must be .xlsx or .csv")
            sheet_renderer = render_xlsx if ext == ".xlsx" else render_csv
            return name, await asyncio.to_thread(sheet_renderer, inp.spreadsheet)
        if inp.action == "create_chart":
            assert inp.chart is not None
            if ext != ".png":
                raise ValidationFailure("charts are written as .png")
            return name, await asyncio.to_thread(render_chart, inp.chart)
        assert inp.content is not None
        return name, render_text(inp.content, name)

    async def _transform(
        self, inp: ForgeInput, ctx: ToolContext, data: bytes, src_name: str
    ) -> tuple[str, bytes, str]:
        ext = extension_of(src_name)
        stem = PurePosixPath(src_name).stem
        src_name = PurePosixPath(src_name).name
        if inp.action == "edit_document":
            assert inp.doc_ops is not None
            if ext != ".docx":
                raise ValidationFailure("edit_document works on .docx files only")
            new = await asyncio.to_thread(edit_docx, data, inp.doc_ops)
            return src_name, new, "formatting inside a replaced paragraph may be simplified"
        if inp.action == "edit_spreadsheet":
            assert inp.sheet_ops is not None
            if ext != ".xlsx":
                raise ValidationFailure("edit_spreadsheet works on .xlsx files only")
            new = await asyncio.to_thread(edit_xlsx, data, inp.sheet_ops)
            return src_name, new, "charts and images in the source workbook are not preserved"
        # convert
        target = inp.convert_to
        if target == "pdf" and ext in {".docx", ".txt", ".md"}:
            parsed = await asyncio.to_thread(
                parse_attachment, data, "docx" if ext == ".docx" else "text", ext
            )
            paragraphs = [Paragraph(text=c.text[:20_000]) for c in parsed.chunks if not c.label]
            spec = DocSpec(
                title=stem[:200] or "Document",
                sections=[Section(blocks=paragraphs[:100] or [Paragraph(text="(empty)")])],
            )
            pdf = await asyncio.to_thread(render_pdf, spec, {})
            return (
                f"{stem}.pdf",
                pdf,
                "text-only conversion; layout, tables, and images are not kept",
            )
        if target == "csv" and ext == ".xlsx":
            wanted = inp.sheet
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
            names = wb.sheetnames
            wb.close()
            sheet_name = wanted or names[0]
            rows = await asyncio.to_thread(read_range, data, sheet_name, "A1:AN2000", 100_000)
            width = max((len(r) for r in rows), default=1)
            header = rows[0] if rows else ["A"]
            cols = [
                Column(name=(header[i] if i < len(header) and header[i] else f"col{i + 1}"))
                for i in range(width)
            ]
            sheet = Sheet(name=sheet_name[:31], columns=cols, rows=[list(r) for r in rows[1:]])
            return (
                f"{stem}.csv",
                render_csv(SheetSpec(sheets=[sheet])),
                "values only; first sheet unless named",
            )
        if target == "xlsx" and ext == ".csv":
            text = data.decode("utf-8-sig", errors="replace")
            parsed_rows = list(csv.reader(io.StringIO(text)))[:5000]
            if not parsed_rows:
                raise ValidationFailure("the CSV is empty")
            width = min(max(len(r) for r in parsed_rows), 100)
            header = parsed_rows[0]
            cols = [
                Column(name=(header[i] if i < len(header) and header[i] else f"col{i + 1}"))
                for i in range(width)
            ]
            sheet = Sheet(
                name="Sheet1", columns=cols, rows=[list(r[:width]) for r in parsed_rows[1:]]
            )
            return (
                f"{stem}.xlsx",
                render_xlsx(SheetSpec(sheets=[sheet])),
                "all values stored as text",
            )
        raise ValidationFailure(f"cannot convert {ext or 'this file'} to {target}")

    async def _images(self, spec: DocSpec, ctx: ToolContext) -> dict[str, bytes]:
        wanted = {b.file_id for s in spec.sections for b in s.blocks if isinstance(b, ImageBlock)}
        found: dict[str, bytes] = {}
        for fid in wanted:
            data, _name, _derived, _ext = await self._load_source(fid, ctx)
            if not (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))):
                raise ValidationFailure(f"{fid} is not a PNG or JPEG image")
            found[fid] = data
        return found

    async def _load_source(
        self, source: str, ctx: ToolContext
    ) -> tuple[bytes, str, list[str], bool]:
        """Return ``(bytes, name, derived_from, external_content)`` for a source reference."""
        assert ctx.services is not None
        store = ctx.services.files
        if source.startswith("att_"):
            rec = await store.get_attachment(source)
            if rec is None or (ctx.chat_id and rec.chat_id != ctx.chat_id):
                raise NotFound("no such attachment in this chat")
            data = await asyncio.to_thread(store.attachment_path(rec).read_bytes)
            return data, rec.safe_name, [rec.id], True
        if source.startswith("file_"):
            row = await store.get_file(source)
            if row is None:
                raise NotFound("no such file")
            version = await store.latest_version(source)
            if version is None:
                raise NotFound("file has no versions")
            data = await store.read_version_bytes(source, version.version)
            return data, str(row["logical_name"]), [source], version.external_content
        # Files made here live under outputs/, and the operator calls them by their plain name
        # ("convert expenses.xlsx", live run 2026-10-25).
        row = await store.find_file(source) or await store.find_file(f"{OUTPUT_DIR}/{source}")
        if row is None:
            raise NotFound(
                f"no file named '{source}' (use an attachment id, a file id, or the name of a "
                "file made in this workspace)"
            )
        version = await store.latest_version(str(row["id"]))
        if version is None:
            raise NotFound("file has no versions")
        data = await store.read_version_bytes(str(row["id"]), version.version)
        return data, str(row["logical_name"]), [str(row["id"])], version.external_content


__all__ = ["DOCX_MIME", "XLSX_MIME", "ArtifactForge"]
