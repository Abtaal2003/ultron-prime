"""Word documents: body, tables, text boxes, headers, footers, footnotes, comments, hidden text.

The XML is walked directly (after ``defusedxml`` validation at upload time) so that text in
content controls, text boxes, and hidden runs is not skipped by a high-level API.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import defusedxml.ElementTree as SafeET

from src.files.parsers import ParsedDoc, split_chunks

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"^(?:Heading|heading)\s*(\d)$|^(Title)$", re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _run_text(run: Any) -> str:
    out: list[str] = []
    for child in run:
        name = _local(child.tag)
        if name == "t" and child.text:
            out.append(child.text)
        elif name == "tab":
            out.append("\t")
        elif name in {"br", "cr"}:
            out.append("\n")
    return "".join(out)


def _is_hidden(run: Any) -> bool:
    props = run.find(f"{W}rPr")
    if props is None:
        return False
    vanish = props.find(f"{W}vanish")
    if vanish is None:
        return False
    return vanish.get(f"{W}val", "true").lower() not in {"0", "false", "off"}


def _runs(node: Any) -> Any:
    """Yield runs under ``node`` without descending into text boxes (handled separately)."""
    for child in node:
        name = _local(child.tag)
        if name == "txbxContent":
            continue
        if name == "r":
            yield child
        yield from _runs(child)


def _paragraph(p: Any) -> tuple[str, str, list[str]]:
    """Return (visible text, style id, hidden run texts); text-box paragraphs are excluded."""
    visible: list[str] = []
    hidden: list[str] = []
    for run in _runs(p):
        text = _run_text(run)
        if text:
            (hidden if _is_hidden(run) else visible).append(text)
    style = ""
    ppr = p.find(f"{W}pPr")
    if ppr is not None:
        pstyle = ppr.find(f"{W}pStyle")
        if pstyle is not None:
            style = pstyle.get(f"{W}val", "")
    return "".join(visible), style, hidden


def _walk(
    el: Any,
    lines: list[str],
    outline: list[str],
    hidden: list[str],
    boxes: list[str],
    in_table: bool = False,
) -> None:
    for child in el:
        name = _local(child.tag)
        if name == "p":
            text, style, hid = _paragraph(child)
            hidden.extend(h for h in hid if h.strip())
            if text.strip():
                lines.append(text)
                m = _HEADING.match(style.replace("Heading", "Heading ").strip())
                if m or style.lower() in {"title", "subtitle"}:
                    level = int(m.group(1)) if m and m.group(1) else 1
                    outline.append(f"{'  ' * (level - 1)}{text.strip()[:120]}")
            for tb in child.iter(f"{W}txbxContent"):
                box_lines: list[str] = []
                _walk(tb, box_lines, [], hidden, boxes)
                if box_lines:
                    boxes.append("\n".join(box_lines))
        elif name == "tbl":
            for tr in child.iter(f"{W}tr"):
                cells = []
                for tc in tr.findall(f"{W}tc"):
                    cell_lines: list[str] = []
                    _walk(tc, cell_lines, [], hidden, boxes, in_table=True)
                    cells.append(" ".join(cell_lines))
                if any(c.strip() for c in cells):
                    lines.append(" | ".join(cells))
        elif name in {"sdt", "sdtContent", "customXml", "smartTag", "ins", "del", "moveTo"}:
            _walk(child, lines, outline, hidden, boxes, in_table)


def _part_text(zf: zipfile.ZipFile, name: str, item: str) -> list[str]:
    root = SafeET.fromstring(zf.read(name))
    out: list[str] = []
    for el in root.iter(f"{W}{item}"):
        lines: list[str] = []
        _walk(el, lines, [], [], [])
        text = "\n".join(lines).strip()
        if text:
            author = el.get(f"{W}author")
            out.append(f"{author}: {text}" if author and item == "comment" else text)
    return out


def parse_docx(data: bytes) -> ParsedDoc:
    """Extract every text layer of a .docx file."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    doc = ParsedDoc(family="docx")
    lines: list[str] = []
    hidden: list[str] = []
    boxes: list[str] = []
    if "word/document.xml" in names:
        root = SafeET.fromstring(zf.read("word/document.xml"))
        body = root.find(f"{W}body")
        if body is not None:
            _walk(body, lines, doc.outline, hidden, boxes)
    body_text = "\n".join(lines)
    chunks = split_chunks(body_text, page=None)
    page = 1
    for chunk in chunks:
        chunk.page = page
        doc.chunks.append(chunk)
        page += 1
    extra: list[tuple[str, str]] = []
    if boxes:
        extra.append(("text box", "\n".join(boxes)))
    if hidden:
        extra.append(("hidden text", "\n".join(hidden)))
        doc.warnings.append("document contains hidden text")
    if "word/comments.xml" in names:
        comments = _part_text(zf, "word/comments.xml", "comment")
        if comments:
            extra.append(("comment", "\n".join(comments)))
            doc.warnings.append("document contains comments")
    for part, item, label in (
        ("word/footnotes.xml", "footnote", "footnote"),
        ("word/endnotes.xml", "endnote", "endnote"),
    ):
        if part in names:
            texts = _part_text(zf, part, item)
            if texts:
                extra.append((label, "\n".join(texts)))
    for name in sorted(names):
        if re.match(r"word/(header|footer)\d*\.xml$", name):
            label = "header" if "header" in name else "footer"
            root = SafeET.fromstring(zf.read(name))
            ls: list[str] = []
            _walk(root, ls, [], [], [])
            if ls:
                extra.append((label, "\n".join(ls)))
    for label, text in extra:
        for chunk in split_chunks(text, page=page, label=label):
            doc.chunks.append(chunk)
        page += 1
    doc.pages = max(page - 1, 1)
    return doc
