"""M4: parsers, renderers, upload validation against hostile files, ingest, forge, reader."""

from __future__ import annotations

import io
import zipfile

import pytest
from pydantic import ValidationError
from src.config import Settings
from src.core.services import Services
from src.files.ingest import FileService, check_batch, requires_quarantine
from src.files.parsers import ParsedDoc, parse_attachment, split_chunks
from src.files.parsers.image import downscale_for_model, normalise_image
from src.files.parsers.pdf import render_pdf_page
from src.files.parsers.xlsx import read_range
from src.files.renderers.chart import render_chart
from src.files.renderers.docx import edit_docx, render_docx
from src.files.renderers.pdf import render_pdf
from src.files.renderers.text import render_text
from src.files.renderers.xlsx import edit_xlsx, render_csv, render_xlsx
from src.files.specs import (
    AppendParagraph,
    AppendRows,
    Bullets,
    ChartSpec,
    CodeBlock,
    Column,
    DocSpec,
    FormulaCell,
    InsertSectionAfter,
    Paragraph,
    RenameSheet,
    ReplaceText,
    Section,
    Series,
    SetCells,
    Sheet,
    SheetSpec,
    Table,
    UpdateTableCell,
    validate_formula,
)
from src.files.validators import UploadRejected, detect_and_validate
from src.tools.attachments import AttachmentReader
from src.tools.forge import ArtifactForge

from tests.fixtures.files import make


# ---------------------------------------------------------------- parsers -----------------------
def test_split_chunks_respects_size_and_offsets() -> None:
    text = "line\n" * 2000
    chunks = split_chunks(text, page=1, max_chars=1000)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert "".join(c.text for c in chunks) == text
    assert chunks[1].start == chunks[0].end
    assert split_chunks("   \n", page=1) == []


def test_text_parser_outline_and_pages() -> None:
    doc = parse_attachment(b"# One\n\ntext\n## Two\n" + b"x" * 7000, "text", ".md")
    assert doc.outline[0] == "One"
    assert doc.outline[1].strip() == "Two"
    assert (doc.pages or 0) >= 2
    py = parse_attachment(b"def f():\n    pass\nclass A:\n    pass\n", "text", ".py")
    assert py.outline == ["def f():", "class A:"]


def test_docx_parser_finds_every_text_layer() -> None:
    doc = parse_attachment(make.docx_with_extras(), "docx", ".docx")
    text = doc.full_text()
    for needle in (
        "Visible paragraph",
        "HIDDEN-PAYLOAD",
        "BOXED-TEXT",
        "COMMENT-PAYLOAD",
        "FOOTNOTE-PAYLOAD",
    ):
        assert needle in text, needle
    labels = {c.label for c in doc.chunks}
    assert {"hidden text", "comment", "text box", "footnote"} <= labels
    assert any("hidden" in w for w in doc.warnings)
    # hidden text must not leak into the visible body chunk
    body = [c for c in doc.chunks if not c.label]
    assert all("HIDDEN-PAYLOAD" not in c.text for c in body)


def test_xlsx_parser_flags_hidden_sheets_comments_and_keeps_formulas_as_text() -> None:
    doc = parse_attachment(make.xlsx_bytes(), "xlsx", ".xlsx")
    text = doc.full_text()
    assert "=SUM(B2:B2)" in text
    assert "HIDDEN-SHEET-TEXT" in text
    assert "COMMENT-CELL" in text
    assert any("hidden" in w for w in doc.warnings)
    assert read_range(make.xlsx_bytes(), "Data", "A1:B2") == [["name", "amount"], ["alpha", "10"]]
    with pytest.raises(KeyError):
        read_range(make.xlsx_bytes(), "Nope", "A1:A2")
    with pytest.raises(ValueError, match="too large"):
        read_range(make.xlsx_bytes(), "Data", "A1:Z200", max_cells=10)


def test_pptx_parser_reads_notes_and_hidden_slides() -> None:
    doc = parse_attachment(make.pptx_bytes(), "pptx", ".pptx")
    text = doc.full_text()
    assert "NOTES-PAYLOAD" in text
    assert "HIDDEN-SLIDE-TEXT" in text
    assert any("hidden" in w for w in doc.warnings)
    assert doc.pages == 2


def test_pdf_parser_pages_and_render() -> None:
    data = make.pdf_bytes(pages=3)
    doc = parse_attachment(data, "pdf", ".pdf")
    assert doc.pages == 3
    assert {c.page for c in doc.chunks} == {1, 2, 3}
    png = render_pdf_page(data, 2)
    assert png.startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="out of range"):
        render_pdf_page(data, 9)


def test_image_normalise_strips_metadata_and_downscales() -> None:
    from PIL import Image

    img = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[0x010E] = "SECRET-DESCRIPTION"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    assert b"SECRET-DESCRIPTION" in buf.getvalue()
    clean, mime = normalise_image(buf.getvalue())
    assert mime == "image/jpeg"
    assert b"SECRET-DESCRIPTION" not in clean
    big = make.png_bytes((3000, 1000))
    small, mime2 = downscale_for_model(big, 500)
    assert mime2 == "image/png"
    assert max(Image.open(io.BytesIO(small)).size) == 500


def test_unreadable_image_parse_info() -> None:
    doc = parse_attachment(make.png_bytes(), "image", ".png")
    assert doc.meta["width"] == 8


# ---------------------------------------------------------------- specs -------------------------
@pytest.mark.parametrize(
    "formula",
    ["=SUM(A1:A3)", "=IF(A1>2,B1,0)", "=AVERAGE(B2:B9)*2", "=MAX(A1,B1)"],
)
def test_formula_allow_list_accepts(formula: str) -> None:
    assert validate_formula(formula) == formula


@pytest.mark.parametrize(
    "formula",
    [
        "SUM(A1)",
        '=HYPERLINK("http://x")',
        "=cmd|' /C calc'!A0",
        "=WEBSERVICE(A1)",
        "=SUM(A1)+EVIL(1)",
        "=IMPORTXML(A1,B1)",
        "='file:///etc/passwd'!A1",
        "=" + "A" * 300,
        "=SUM(A1);DROP",
        "=A1\\\\server\\share",
    ],
)
def test_formula_allow_list_rejects(formula: str) -> None:
    with pytest.raises(ValueError):
        validate_formula(formula)
    with pytest.raises(ValidationError):
        FormulaCell(formula=formula)


def test_spec_validation_limits() -> None:
    with pytest.raises(ValidationError):
        Sheet(name="bad/name", columns=[Column(name="a")])
    with pytest.raises(ValidationError):
        SetCells(sheet="S", cells={"not a ref": 1})
    with pytest.raises(ValidationError):
        Table(header=[f"c{i}" for i in range(31)])
    with pytest.raises(ValidationError):
        Section(blocks=[{"kind": "script", "text": "x"}])  # type: ignore[list-item]


# ---------------------------------------------------------------- renderers ---------------------
def _doc_spec() -> DocSpec:
    return DocSpec(
        title="Quarterly <Report> & more",
        subtitle="Draft",
        sections=[
            Section(
                heading="Intro",
                blocks=[
                    Paragraph(text="Hello world\x00\x07 <b>not markup</b>"),
                    Bullets(items=["one", "two"], ordered=True),
                    Table(header=["k", "v"], rows=[["a", "1"], ["b"]]),
                    CodeBlock(text="print('x')"),
                ],
            ),
            Section(heading="End", blocks=[Paragraph(text="Bye")]),
        ],
    )


def test_docx_render_roundtrip_and_clean_control_chars() -> None:
    data = render_docx(_doc_spec())
    text = parse_attachment(data, "docx", ".docx").full_text()
    assert "Quarterly <Report> & more" in text
    assert "Hello world" in text
    assert "\x00" not in text
    detect_and_validate(data, "r.docx", Settings(_env_file=None))


def test_pdf_render_roundtrip() -> None:
    data = render_pdf(_doc_spec())
    doc = parse_attachment(data, "pdf", ".pdf")
    assert "Hello world" in doc.full_text()
    detect_and_validate(data, "r.pdf", Settings(_env_file=None))


def test_docx_edit_ops() -> None:
    base = render_docx(_doc_spec())
    edited = edit_docx(
        base,
        [
            ReplaceText(find="Hello", replace="Howdy"),
            UpdateTableCell(table_index=0, row=1, col=1, text="99"),
            InsertSectionAfter(
                after_heading="Intro",
                section=Section(heading="Middle", blocks=[Paragraph(text="between")]),
            ),
            AppendParagraph(text="tail"),
        ],
    )
    text = parse_attachment(edited, "docx", ".docx").full_text()
    assert "Howdy world" in text
    assert "a | 99" in text
    assert text.index("Intro") < text.index("Middle") < text.index("End")
    assert text.rstrip().endswith("tail")
    with pytest.raises(Exception, match="not found"):
        edit_docx(base, [ReplaceText(find="zzzz", replace="y")])
    with pytest.raises(Exception, match="no heading"):
        edit_docx(
            base,
            [
                InsertSectionAfter(
                    after_heading="Nope", section=Section(blocks=[Paragraph(text="x")])
                )
            ],
        )
    with pytest.raises(Exception, match="does not exist"):
        edit_docx(base, [UpdateTableCell(table_index=4, row=0, col=0, text="x")])


def _sheet_spec() -> SheetSpec:
    return SheetSpec(
        sheets=[
            Sheet(
                name="Data",
                columns=[Column(name="name"), Column(name="amount", type="currency")],
                rows=[
                    ["=cmd|'/C calc'!A0", 5],
                    ["+SUM(1)", 7.5],
                    ["ok", FormulaCell(formula="=SUM(B2:B3)")],
                ],
            )
        ]
    )


def test_xlsx_formula_text_is_never_a_formula_but_real_formulas_are() -> None:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(render_xlsx(_sheet_spec())))
    ws = wb["Data"]
    assert ws["A2"].data_type == "s"
    assert ws["A2"].value.startswith("=cmd")
    assert ws["A3"].data_type == "s"
    assert ws["B4"].data_type == "f"
    assert ws.freeze_panes == "A2"


def test_csv_neutralises_formula_triggers() -> None:
    rows = render_csv(_sheet_spec()).decode().splitlines()
    assert rows[1].startswith("'=cmd")
    assert rows[2].startswith("'+SUM")
    assert "'=SUM(B2:B3)" in rows[3]


def test_xlsx_edit_ops_and_errors() -> None:
    base = render_xlsx(_sheet_spec())
    edited = edit_xlsx(
        base,
        [
            SetCells(sheet="Data", cells={"A5": "=evil()", "B5": 3}),
            AppendRows(sheet="Data", rows=[["z", 1]]),
            RenameSheet(from_name="Data", to_name="Main"),
        ],
    )
    text = parse_attachment(edited, "xlsx", ".xlsx").full_text()
    assert "Sheet: Main" in text
    assert "=evil()" in text
    with pytest.raises(Exception, match="no sheet"):
        edit_xlsx(base, [SetCells(sheet="Nope", cells={"A1": 1})])
    with pytest.raises(Exception, match="already exists"):
        edit_xlsx(
            base,
            [RenameSheet(from_name="Data", to_name="Data")],
        )


def test_sheet_size_and_shape_checks() -> None:
    wide = SheetSpec(sheets=[Sheet(name="S", columns=[Column(name="a")], rows=[[1, 2]])])
    with pytest.raises(Exception, match="wider"):
        render_xlsx(wide)
    dup = SheetSpec(
        sheets=[
            Sheet(name="S", columns=[Column(name="a")]),
            Sheet(name="s", columns=[Column(name="a")]),
        ]
    )
    with pytest.raises(Exception, match="unique"):
        render_xlsx(dup)


@pytest.mark.parametrize("kind", ["line", "bar", "scatter", "histogram", "pie"])
def test_charts_render_png(kind: str) -> None:
    spec = ChartSpec(
        type=kind,  # type: ignore[arg-type]
        title="t",
        categories=["a", "b", "c"],
        series=[Series(name="s", y=[1, 2, 3]), Series(name="u", y=[3, 2, 1])]
        if kind != "pie"
        else [Series(y=[1, 2, 3])],
    )
    assert render_chart(spec).startswith(b"\x89PNG")


def test_chart_validation_errors() -> None:
    with pytest.raises(Exception, match="negative"):
        render_chart(ChartSpec(type="pie", categories=["a"], series=[Series(y=[-1])]))
    with pytest.raises(Exception, match="one value per category"):
        render_chart(ChartSpec(type="bar", categories=["a", "b"], series=[Series(y=[1])]))


def test_text_renderer() -> None:
    assert render_text("hi", "a.txt") == b"hi\n"
    assert b'"a": 1' in render_text('{"a":1}', "a.json")
    with pytest.raises(Exception, match="invalid JSON"):
        render_text("{", "a.json")
    with pytest.raises(Exception, match="NUL"):
        render_text("a\x00b", "a.txt")
    with pytest.raises(Exception, match="not a text"):
        render_text("x", "a.exe")


# ---------------------------------------------------------------- hostile uploads ---------------
@pytest.fixture
def cfg() -> Settings:
    return Settings(_env_file=None)


HOSTILE = [
    ("zip bomb docx", make.zip_bomb_docx, "a.docx", "zip bomb"),
    ("xxe docx", make.xxe_docx, "a.docx", "XXE"),
    ("billion laughs xlsx", make.billion_laughs_xlsx, "a.xlsx", "XXE"),
    ("macro docx", make.macro_docx, "a.docx", "macros"),
    ("png decompression bomb", make.png_decompression_bomb, "a.png", "bomb"),
    ("pdf/zip polyglot", make.pdf_zip_polyglot, "a.pdf", "polyglot"),
    ("png renamed pdf", make.png_bytes, "a.pdf", "really a PNG"),
]


@pytest.mark.parametrize(
    ("label", "builder", "name", "reason"), HOSTILE, ids=[h[0] for h in HOSTILE]
)
def test_hostile_files_are_rejected_with_a_reason(
    cfg: Settings,
    label: str,
    builder,
    name: str,
    reason: str,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(UploadRejected, match=reason):
        detect_and_validate(builder(), name, cfg)


@pytest.mark.parametrize(
    ("data", "name", "reason"),
    [
        (b"", "a.txt", "empty"),
        (b"MZ\x90\x00", "a.exe", "executables"),
        (b"PK\x03\x04", "a.zip", "archives"),
        (b"abc\x00def", "a.txt", "binary"),
        (b"text", "noext", "not an accepted"),
        (b"text", "a.svg", "SVG"),
    ],
)
def test_simple_rejections(cfg: Settings, data: bytes, name: str, reason: str) -> None:
    with pytest.raises(UploadRejected, match=reason):
        detect_and_validate(data, name, cfg)


def test_oversized_upload_and_pdf_pages(cfg: Settings) -> None:
    small = Settings(_env_file=None, upload_max_mb=1, upload_max_mb_per_message=5)
    with pytest.raises(UploadRejected, match="larger than 1 MB"):
        detect_and_validate(b"x" * (1024 * 1024 + 1), "a.txt", small)
    few = Settings(_env_file=None, upload_max_pdf_pages=2)
    with pytest.raises(UploadRejected, match="pages"):
        detect_and_validate(make.pdf_bytes(pages=3), "a.pdf", few)
    ok = detect_and_validate(make.pdf_bytes(pages=2), "a.pdf", few)
    assert ok.pages == 2


def test_pdf_javascript_is_flagged_not_run(cfg: Settings) -> None:
    result = detect_and_validate(make.pdf_with_javascript(), "a.pdf", cfg)
    assert "pdf_active_content_ignored" in result.warnings


def test_bidi_filename_is_sanitised(cfg: Settings) -> None:
    from src.tools.security import sanitize_filename

    assert "\u202e" not in sanitize_filename("invoice\u202egpj.exe")
    assert "/" not in sanitize_filename("../../etc/passwd")


def test_batch_limits() -> None:
    cfg = Settings(
        _env_file=None, upload_max_files_per_message=2, upload_max_mb=1, upload_max_mb_per_message=2
    )
    check_batch(cfg, [10, 10])
    with pytest.raises(UploadRejected, match="at most 2"):
        check_batch(cfg, [1, 1, 1])
    with pytest.raises(UploadRejected, match="together"):
        check_batch(cfg, [1_500_000, 1_000_000])


def test_requires_quarantine_modes() -> None:
    std = Settings(_env_file=None)
    assert requires_quarantine(std, "pdf")
    assert requires_quarantine(std, "docx")
    assert not requires_quarantine(std, "text")
    assert not requires_quarantine(std, "image")
    strict = Settings(_env_file=None, quarantine_mode="strict")
    assert requires_quarantine(strict, "text")
    off = Settings(_env_file=None, quarantine_mode="off")
    assert not requires_quarantine(off, "pdf")
    no_uploads = Settings(_env_file=None, quarantine_sources=["web"])
    assert not requires_quarantine(no_uploads, "pdf")


# ---------------------------------------------------------------- ingest ------------------------
@pytest.fixture
def ingest(services: Services) -> FileService:
    return FileService(services.files, services.settings, services.scanner)


async def test_ingest_pdf_stores_chunks_and_immutable_original(
    ingest: FileService, services: Services, chat: str
) -> None:
    result = await ingest.ingest(chat_id=chat, filename="Report.pdf", data=make.pdf_bytes(pages=2))
    assert result.family == "pdf"
    assert result.quarantine is True
    assert result.record.pages == 2
    chunks = await services.files.get_chunks(result.record.id)
    assert len(chunks) == 2
    path = services.files.attachment_path(result.record)
    assert not path.stat().st_mode & 0o222  # read-only
    only_page_two = await services.files.get_chunks(result.record.id, [2])
    assert len(only_page_two) == 1


async def test_ingest_text_is_not_quarantined_and_dedupes_blob(
    ingest: FileService, services: Services, chat: str
) -> None:
    a = await ingest.ingest(chat_id=chat, filename="a.txt", data=b"same bytes")
    b = await ingest.ingest(chat_id=chat, filename="b.txt", data=b"same bytes")
    assert a.quarantine is False
    assert a.record.id != b.record.id
    assert a.record.sha256 == b.record.sha256


async def test_ingest_flags_secrets_without_echoing(ingest: FileService, chat: str) -> None:
    result = await ingest.ingest(
        chat_id=chat, filename="env.txt", data=b"token = sk-ant-api03-" + b"A" * 40
    )
    assert result.secret_warning
    assert any("credentials" in w for w in result.warnings)


async def test_ingest_image_strips_metadata(
    ingest: FileService, services: Services, chat: str
) -> None:
    from PIL import Image

    exif = Image.Exif()
    exif[0x010E] = "GPS-LEAK"
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="JPEG", exif=exif.tobytes())
    result = await ingest.ingest(chat_id=chat, filename="p.jpg", data=buf.getvalue())
    stored = services.files.attachment_path(result.record).read_bytes()
    assert b"GPS-LEAK" not in stored
    assert result.record.width == 10


async def test_ingest_rejects_hostile_and_stores_nothing(
    ingest: FileService, services: Services, chat: str
) -> None:
    with pytest.raises(UploadRejected):
        await ingest.ingest(chat_id=chat, filename="x.docx", data=make.xxe_docx())
    assert await services.files.list_attachments(chat) == []


async def test_ingest_unparseable_document_is_rejected(ingest: FileService, chat: str) -> None:
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<not-closed")
    with pytest.raises(UploadRejected):
        await ingest.ingest(chat_id=chat, filename="broken.docx", data=broken.getvalue())


# ---------------------------------------------------------------- forge -------------------------
async def test_forge_creates_each_kind_and_versions(
    ctx_factory, services: Services, chat: str
) -> None:  # type: ignore[no-untyped-def]
    forge = ArtifactForge()
    ctx = ctx_factory()
    r = await forge.execute(
        {"action": "create_document", "filename": "r.docx", "document": _doc_spec().model_dump()},
        ctx,
    )
    assert r.ok, r.error
    assert r.files[0].logical_name == "outputs/r.docx"
    assert (services.settings.workspace_dir / "outputs" / "r.docx").exists()
    r2 = await forge.execute(
        {"action": "create_document", "filename": "r.docx", "document": _doc_spec().model_dump()},
        ctx,
    )
    assert r2.data is not None
    assert r2.data["version"] == 2
    for payload in (
        {
            "action": "create_spreadsheet",
            "filename": "s.xlsx",
            "spreadsheet": _sheet_spec().model_dump(),
        },
        {
            "action": "create_spreadsheet",
            "filename": "s.csv",
            "spreadsheet": _sheet_spec().model_dump(),
        },
        {
            "action": "create_chart",
            "filename": "c.png",
            "chart": {"type": "line", "series": [{"y": [1, 2, 3]}]},
        },
        {"action": "create_text", "filename": "n.md", "content": "# hi"},
        {"action": "create_document", "filename": "r.pdf", "document": _doc_spec().model_dump()},
    ):
        res = await forge.execute(payload, ctx)
        assert res.ok, (payload["action"], res.error)


async def test_forge_rejects_bad_requests(ctx_factory, chat: str) -> None:  # type: ignore[no-untyped-def]
    forge = ArtifactForge()
    ctx = ctx_factory()
    cases = [
        ({"action": "create_document", "filename": "r.docx"}, "requires"),
        ({"action": "create_text", "filename": "x.exe", "content": "x"}, ""),
        (
            {
                "action": "create_chart",
                "filename": "c.jpg",
                "chart": {"type": "line", "series": [{"y": [1]}]},
            },
            "png",
        ),
        (
            {
                "action": "create_document",
                "filename": "r.txt",
                "document": _doc_spec().model_dump(),
            },
            "docx",
        ),
        ({"action": "create_text", "filename": "../../etc/x.txt", "content": "ok"}, None),
    ]
    for payload, fragment in cases:
        res = await forge.execute(payload, ctx)
        if fragment is None:
            assert res.ok  # path parts are stripped, not honoured
            assert res.files[0].logical_name == "outputs/x.txt"
        else:
            assert not res.ok
            assert (
                fragment in (res.error.message + (res.error.detail or "")) if res.error else False
            )


async def test_forge_refuses_credentials(ctx_factory, chat: str) -> None:  # type: ignore[no-untyped-def]
    res = await ArtifactForge().execute(
        {"action": "create_text", "filename": "k.txt", "content": "key sk-ant-api03-" + "B" * 40},
        ctx_factory(),
    )
    assert not res.ok
    assert res.error is not None
    assert res.error.kind == "policy"


async def test_forge_edit_of_upload_leaves_original_untouched(
    ctx_factory,
    services: Services,
    ingest: FileService,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    upload = await ingest.ingest(chat_id=chat, filename="in.docx", data=render_docx(_doc_spec()))
    before = services.files.attachment_path(upload.record).read_bytes()
    res = await ArtifactForge().execute(
        {
            "action": "edit_document",
            "source": upload.record.id,
            "doc_ops": [{"op": "replace_text", "find": "Hello", "replace": "Howdy"}],
        },
        ctx_factory(),
    )
    assert res.ok, res.error
    assert services.files.attachment_path(upload.record).read_bytes() == before
    assert res.files[0].external_content is True
    assert res.files[0].derived_from == [upload.record.id]
    assert res.files[0].logical_name == "outputs/in.docx"


async def test_forge_edit_workspace_file_makes_new_version(
    ctx_factory,
    services: Services,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    forge = ArtifactForge()
    ctx = ctx_factory()
    await forge.execute(
        {
            "action": "create_spreadsheet",
            "filename": "s.xlsx",
            "spreadsheet": _sheet_spec().model_dump(),
        },
        ctx,
    )
    res = await forge.execute(
        {
            "action": "edit_spreadsheet",
            "source": "outputs/s.xlsx",
            "sheet_ops": [{"op": "append_rows", "sheet": "Data", "rows": [["new", 1]]}],
        },
        ctx,
    )
    assert res.ok, res.error
    assert res.data is not None
    assert res.data["version"] == 2
    assert res.files[0].logical_name == "outputs/s.xlsx"


async def test_forge_convert_variants(ctx_factory, chat: str) -> None:  # type: ignore[no-untyped-def]
    forge = ArtifactForge()
    ctx = ctx_factory()
    await forge.execute(
        {"action": "create_document", "filename": "d.docx", "document": _doc_spec().model_dump()},
        ctx,
    )
    await forge.execute(
        {
            "action": "create_spreadsheet",
            "filename": "s.xlsx",
            "spreadsheet": _sheet_spec().model_dump(),
        },
        ctx,
    )
    await forge.execute(
        {"action": "create_text", "filename": "t.csv", "content": "a,b\n1,2\n"}, ctx
    )
    for source, target, expect in (
        ("outputs/d.docx", "pdf", "outputs/d.pdf"),
        ("outputs/s.xlsx", "csv", "outputs/s.csv"),
        ("outputs/t.csv", "xlsx", "outputs/t.xlsx"),
    ):
        res = await forge.execute(
            {"action": "convert", "source": source, "convert_to": target}, ctx
        )
        assert res.ok, (source, res.error)
        assert res.files[0].logical_name == expect
    bad = await forge.execute(
        {"action": "convert", "source": "outputs/t.csv", "convert_to": "pdf"}, ctx
    )
    assert not bad.ok
    missing = await forge.execute(
        {"action": "convert", "source": "nope.docx", "convert_to": "pdf"}, ctx
    )
    assert not missing.ok
    assert missing.error is not None
    assert missing.error.kind == "not_found"


async def test_forge_image_block_from_attachment(
    ctx_factory,
    ingest: FileService,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    from src.files.specs import ImageBlock

    img = await ingest.ingest(chat_id=chat, filename="i.png", data=make.png_bytes((50, 50)))
    spec = DocSpec(
        title="With image", sections=[Section(blocks=[ImageBlock(file_id=img.record.id)])]
    )
    res = await ArtifactForge().execute(
        {"action": "create_document", "filename": "i.docx", "document": spec.model_dump()},
        ctx_factory(),
    )
    assert res.ok, res.error
    missing = DocSpec(title="x", sections=[Section(blocks=[ImageBlock(file_id="att_nope")])])
    res2 = await ArtifactForge().execute(
        {"action": "create_document", "filename": "j.docx", "document": missing.model_dump()},
        ctx_factory(),
    )
    assert not res2.ok


# ---------------------------------------------------------------- attachment_reader -------------
async def test_reader_list_read_find_range_outline(
    ctx_factory,
    ingest: FileService,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    reader = AttachmentReader()
    ctx = ctx_factory()
    pdf = await ingest.ingest(
        chat_id=chat, filename="d.pdf", data=make.pdf_bytes(pages=3, text="needle")
    )
    xls = await ingest.ingest(chat_id=chat, filename="s.xlsx", data=make.xlsx_bytes())
    txt = await ingest.ingest(chat_id=chat, filename="t.txt", data=b"plain words here")

    listing = await reader.execute({"op": "list"}, ctx)
    assert listing.ok
    assert listing.data is not None
    assert len(listing.data["attachments"]) == 3
    assert listing.taints_turn is False

    read = await reader.execute({"op": "read", "attachment_id": pdf.record.id, "pages": [2]}, ctx)
    assert read.ok
    assert read.data is not None
    assert read.data["passages"][0]["page"] == 2
    assert read.taints_turn is True
    assert read.quarantine is True

    found = await reader.execute(
        {"op": "find", "attachment_id": pdf.record.id, "query": "NEEDLE"}, ctx
    )
    assert found.data is not None
    assert len(found.data["passages"]) == 3

    rng = await reader.execute(
        {"op": "range", "attachment_id": xls.record.id, "sheet": "Data", "cell_range": "a1:b2"}, ctx
    )
    assert rng.data is not None
    assert rng.data["rows"][1] == ["alpha", "10"]

    outline = await reader.execute({"op": "outline", "attachment_id": xls.record.id}, ctx)
    assert outline.ok
    assert outline.data is not None
    assert outline.data["outline"][0].startswith("Data")

    plain = await reader.execute({"op": "read", "attachment_id": txt.record.id}, ctx)
    assert plain.taints_turn is False
    assert plain.quarantine is False


async def test_reader_truncation_page_image_and_errors(
    ctx_factory,
    ingest: FileService,
    services: Services,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    reader = AttachmentReader()
    ctx = ctx_factory()
    big = await ingest.ingest(chat_id=chat, filename="big.txt", data=(b"word " * 20000))
    res = await reader.execute(
        {"op": "read", "attachment_id": big.record.id, "max_chars": 1000}, ctx
    )
    assert res.truncated
    assert sum(len(p["text"]) for p in res.data["passages"]) <= 1000  # type: ignore[index]

    pdf = await ingest.ingest(chat_id=chat, filename="d.pdf", data=make.pdf_bytes(pages=2))
    img = await reader.execute({"op": "page_image", "attachment_id": pdf.record.id, "page": 2}, ctx)
    assert img.ok, img.error
    assert img.data is not None
    stored = await services.files.get_attachment(img.data["image_attachment_id"])
    assert stored is not None
    assert stored.detected_mime == "image/png"
    out_of_range = await reader.execute(
        {"op": "page_image", "attachment_id": pdf.record.id, "page": 9}, ctx
    )
    assert not out_of_range.ok

    other_chat = ctx_factory(chat_id="someone_else")
    denied = await reader.execute({"op": "read", "attachment_id": pdf.record.id}, other_chat)
    assert not denied.ok
    assert denied.error is not None
    assert denied.error.kind == "not_found"
    bad = await reader.execute({"op": "read"}, ctx)
    assert not bad.ok
    wrong = await reader.execute(
        {"op": "range", "attachment_id": pdf.record.id, "sheet": "A", "cell_range": "A1:B2"}, ctx
    )
    assert not wrong.ok


async def test_reader_redacts_secrets_in_flagged_files(
    ctx_factory,
    ingest: FileService,
    chat: str,  # type: ignore[no-untyped-def]
) -> None:
    secret = "sk-ant-api03-" + "Z" * 40
    up = await ingest.ingest(
        chat_id=chat, filename="env.txt", data=f"token = {secret}\nhello".encode()
    )
    res = await AttachmentReader().execute(
        {"op": "read", "attachment_id": up.record.id}, ctx_factory()
    )
    assert res.ok
    assert secret not in str(res.data)
    assert "hello" in str(res.data)


def test_parsed_doc_helpers() -> None:
    doc = ParsedDoc(family="x", chunks=split_chunks("abc", 1, label="note"))
    assert doc.total_chars == 3
    assert doc.full_text() == "[note] abc"


def test_pdf_annotation_text_is_extracted() -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import FreeText, Text

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make.pdf_bytes())))
    writer.add_annotation(0, Text(text="ANNOT-NOTE-PAYLOAD", rect=(50, 50, 100, 100)))
    writer.add_annotation(0, FreeText(text="FREETEXT-PAYLOAD", rect=(50, 150, 300, 200)))
    out = io.BytesIO()
    writer.write(out)
    doc = parse_attachment(out.getvalue(), "pdf", ".pdf")
    annotated = [c for c in doc.chunks if c.label == "annotation"]
    joined = " ".join(c.text for c in annotated)
    assert "ANNOT-NOTE-PAYLOAD" in joined
    assert "FREETEXT-PAYLOAD" in joined
