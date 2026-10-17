"""Builders for benign and hostile sample files (generated on the fly, never committed)."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

from docx import Document
from openpyxl import Workbook
from openpyxl.comments import Comment
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from reportlab.pdfgen import canvas


def docx_bytes(paragraphs: list[str] | None = None) -> bytes:
    doc = Document()
    for text in paragraphs or ["Hello from a document."]:
        doc.add_paragraph(text)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def docx_with_extras() -> bytes:
    """A .docx with hidden text, a comment part, a footnote part, and a header."""
    base = docx_bytes(["Visible paragraph."])
    src = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode()
                hidden = "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN-PAYLOAD</w:t></w:r></w:p>"
                box = (
                    '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
                    'markup-compatibility/2006"><mc:Choice><w:pict><v:shape xmlns:v="urn:schemas-'
                    'microsoft-com:vml"><v:textbox><w:txbxContent><w:p><w:r><w:t>BOXED-TEXT</w:t>'
                    "</w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></mc:Choice>"
                    "</mc:AlternateContent></w:r></w:p>"
                )
                text = text.replace("</w:body>", hidden + box + "</w:body>")
                data = text.encode()
            dst.writestr(item, data)
        dst.writestr(
            "word/comments.xml",
            '<?xml version="1.0"?><w:comments xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:comment w:id="0" w:author="Eve"><w:p><w:r>'
            "<w:t>COMMENT-PAYLOAD</w:t></w:r></w:p></w:comment></w:comments>",
        )
        dst.writestr(
            "word/footnotes.xml",
            '<?xml version="1.0"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>FOOTNOTE-PAYLOAD'
            "</w:t></w:r></w:p></w:footnote></w:footnotes>",
        )
    return out.getvalue()


def xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Data"
    ws.append(["name", "amount"])
    ws.append(["alpha", 10])
    ws.append(["beta", "=SUM(B2:B2)"])
    ws["A2"].comment = Comment("COMMENT-CELL", "eve")
    secret = wb.create_sheet("Secret")
    secret.append(["HIDDEN-SHEET-TEXT"])
    secret.sheet_state = "hidden"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def pptx_bytes() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Title slide"  # type: ignore[union-attr]
    slide.placeholders[1].text = "Body text"
    slide.notes_slide.notes_text_frame.text = "NOTES-PAYLOAD"  # type: ignore[union-attr]
    hidden = prs.slides.add_slide(prs.slide_layouts[5])
    hidden.shapes.title.text = "Hidden one"  # type: ignore[union-attr]
    hidden._element.set("show", "0")
    tb = hidden.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    tb.text_frame.text = "HIDDEN-SLIDE-TEXT"
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def pdf_bytes(pages: int = 1, text: str = "Page text") -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out)
    for i in range(pages):
        c.drawString(72, 720, f"{text} {i + 1}")
        c.showPage()
    c.save()
    return out.getvalue()


def png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def png_decompression_bomb(width: int = 30_000, height: int = 30_000) -> bytes:
    """A tiny PNG whose header claims a huge canvas (never decoded by the validator)."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00" * 64)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def zip_bomb_docx(ratio_mb: int = 300) -> bytes:
    base = docx_bytes()
    src = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/media/bomb.bin", b"\x00" * (ratio_mb * 1024 * 1024))
    return out.getvalue()


def xxe_docx() -> bytes:
    base = docx_bytes()
    src = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                data = (
                    b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                    b'2006/main"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'
                )
            dst.writestr(item, data)
    return out.getvalue()


def billion_laughs_xlsx() -> bytes:
    base = xlsx_bytes()
    src = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    entities = "".join(
        f'<!ENTITY l{i} "{f"&l{i - 1};" * 10}">' if i else '<!ENTITY l0 "lol">' for i in range(6)
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/workbook.xml":
                data = f'<?xml version="1.0"?><!DOCTYPE w [{entities}]>'.encode() + data.split(
                    b"?>", 1
                )[-1].replace(b"<workbook", b"<workbook", 1)
            dst.writestr(item, data)
    return out.getvalue()


def macro_docx() -> bytes:
    base = docx_bytes()
    src = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/vbaProject.bin", b"MACRO")
    return out.getvalue()


def pdf_with_javascript() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes())))
    writer.add_js("app.alert('x')")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_zip_polyglot() -> bytes:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("a.txt", "x")
    return pdf_bytes() + zbuf.getvalue()
