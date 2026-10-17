"""Tool 4: ``file_system_scribe`` (spec 11.5). Confined, versioned text-file operations.

Confinement is enforced by :func:`src.tools.security.confine_path` (resolved-path containment).
Writes go through the :class:`~src.files.store.FileStore`, so ``overwrite`` creates a new version
and keeps the old bytes. There is deliberately **no delete** operation in v1.
"""

from __future__ import annotations

import ast
import json
import os
import tomllib
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from src.core.errors import Conflict, LimitExceeded, NotFound, PolicyDenied, ValidationFailure
from src.files.types import SCRIBE_EXTENSIONS, TEXT_MIME_BY_EXT, extension_of
from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput
from src.tools.security import confine_path, is_control_or_format, relative_to_root

READ_LIMIT = 1 * 1024 * 1024
WRITE_LIMIT = 2 * 1024 * 1024
LIST_LIMIT = 200
MAX_DEPTH = 12

Op = Literal[
    "read", "write", "append", "list", "exists", "stat", "mkdir", "compile_check", "analyze"
]


class ScribeInput(StrictModel):
    """One scribe operation. Which fields apply depends on ``op``."""

    op: Op
    path: str = Field("", max_length=512, description="Workspace-relative path, e.g. 'notes/a.md'.")
    content: str | None = Field(None, description="Text for write and append (UTF-8).")
    overwrite: bool = Field(
        False, description="Allow write to replace an existing file (versioned)."
    )

    @model_validator(mode="after")
    def _check(self) -> ScribeInput:
        if self.op in {"write", "append"} and self.content is None:
            raise ValueError(f"'content' is required for op={self.op}")
        if self.op not in {"write", "append"} and self.content is not None:
            raise ValueError("'content' is only valid for write and append")
        if self.op != "list" and not self.path:
            raise ValueError("'path' is required")
        return self


class Entry(BaseModel):
    """A directory entry."""

    name: str
    kind: Literal["file", "dir", "symlink", "other"]
    size: int | None = None
    modified_epoch: float | None = None


class ScribeOutput(BaseModel):
    """Result of a scribe operation; unused fields are ``None``."""

    op: str
    path: str
    exists: bool | None = None
    content: str | None = None
    entries: list[Entry] | None = None
    size: int | None = None
    kind: str | None = None
    modified_epoch: float | None = None
    bytes_written: int | None = None
    version: int | None = None
    file_id: str | None = None
    created_dir: bool | None = None
    check: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None


def _kind(path: Path) -> Literal["file", "dir", "symlink", "other"]:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    if path.is_file():
        return "file"
    return "other"


def _check_extension(path: str) -> str:
    ext = extension_of(path)
    if ext not in SCRIBE_EXTENSIONS:
        allowed = " ".join(sorted(SCRIBE_EXTENSIONS))
        raise PolicyDenied(
            f"extension '{ext or '(none)'}' is not allowed here; text formats only: {allowed}. "
            "Binary formats go through artifact_forge."
        )
    return ext


def _check_depth(path: str) -> None:
    if len([p for p in path.replace("\\", "/").split("/") if p]) > MAX_DEPTH:
        raise PolicyDenied("path is nested too deeply")


def read_text_limited(path: Path, limit: int) -> tuple[str, bool, int]:
    """Read up to ``limit`` bytes (no final symlink); return text, truncated flag, size."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        raw = handle.read(limit + 1)
    truncated = len(raw) > limit
    raw = raw[:limit]
    if b"\x00" in raw:
        raise ValidationFailure("file contains NUL bytes and is not text")
    return raw.decode("utf-8", errors="replace"), truncated, size


def compile_check(ext: str, text: str) -> dict[str, Any]:
    """Syntax-check text without executing it."""
    try:
        if ext == ".py":
            ast.parse(text)
        elif ext == ".json":
            json.loads(text)
        elif ext in {".yaml", ".yml"}:
            list(yaml.safe_load_all(text))
        elif ext == ".toml":
            tomllib.loads(text)
        else:
            return {"supported": False, "ok": None, "message": f"no checker for {ext}"}
    except SyntaxError as exc:
        return {
            "supported": True,
            "ok": False,
            "line": exc.lineno,
            "column": exc.offset,
            "message": exc.msg,
        }
    except json.JSONDecodeError as exc:
        return {
            "supported": True,
            "ok": False,
            "line": exc.lineno,
            "column": exc.colno,
            "message": exc.msg,
        }
    except (yaml.YAMLError, tomllib.TOMLDecodeError, ValueError) as exc:
        return {
            "supported": True,
            "ok": False,
            "line": None,
            "column": None,
            "message": str(exc)[:300],
        }
    return {"supported": True, "ok": True, "message": "syntax ok"}


def _json_depth(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + max((_json_depth(v) for v in node.values()), default=0)
    if isinstance(node, list):
        return 1 + max((_json_depth(v) for v in node), default=0)
    return 0


def analyze_text(ext: str, text: str) -> dict[str, Any]:
    """Return structural statistics for a text file."""
    lines = text.splitlines()
    info: dict[str, Any] = {
        "lines": len(lines),
        "characters": len(text),
        "longest_line": max((len(line) for line in lines), default=0),
        "blank_lines": sum(1 for line in lines if not line.strip()),
        "todo_markers": sum(text.count(m) for m in ("TODO", "FIXME", "XXX")),
        "non_printing_characters": sum(
            1 for ch in text if is_control_or_format(ch) and ch not in "\n\r\t"
        ),
    }
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            info["python"] = {"syntax_error": f"line {exc.lineno}: {exc.msg}"}
            return info
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        imports = sorted(
            {
                (a.name if isinstance(n, ast.Import) else (n.module or ""))
                for n in ast.walk(tree)
                if isinstance(n, ast.Import | ast.ImportFrom)
                for a in (
                    n.names if isinstance(n, ast.Import) else [ast.alias(name=n.module or "")]
                )
            }
        )
        longest = max(
            ((f.name, (f.end_lineno or f.lineno) - f.lineno + 1) for f in funcs),
            key=lambda t: t[1],
            default=None,
        )
        info["python"] = {
            "functions": len(funcs),
            "classes": len(classes),
            "imports": imports[:50],
            "longest_function": {"name": longest[0], "lines": longest[1]} if longest else None,
            "functions_without_docstring": sum(1 for f in funcs if ast.get_docstring(f) is None),
        }
    elif ext == ".json":
        try:
            data = json.loads(text)
            info["json"] = {
                "root_type": type(data).__name__,
                "depth": _json_depth(data),
                "top_level_keys": list(data)[:30] if isinstance(data, dict) else None,
                "items": len(data) if isinstance(data, list | dict) else None,
            }
        except json.JSONDecodeError as exc:
            info["json"] = {"syntax_error": f"line {exc.lineno}: {exc.msg}"}
    elif ext in {".csv", ".tsv"}:
        sep = "\t" if ext == ".tsv" else ","
        info["table"] = {
            "rows": max(len(lines) - 1, 0),
            "columns": len(lines[0].split(sep)) if lines else 0,
        }
    return info


class FileSystemScribe(Tool):
    """Confined, versioned text-file operations inside the workspace."""

    name: ClassVar[str] = "file_system_scribe"
    description: ClassVar[str] = (
        "Read, write, append, list, stat, syntax-check, and analyse TEXT files inside the "
        "operator's workspace (paths are workspace-relative; absolute paths and '..' are refused). "
        "Overwriting an existing file requires overwrite=true and keeps the previous version. "
        "There is no delete. For Word, Excel, PDF, or charts use artifact_forge instead. "
        'Examples: {"op": "read", "path": "notes/todo.md"}; '
        '{"op": "write", "path": "src/app.py", "content": "...", "overwrite": false}.'
    )
    input_model: ClassVar[type[StrictModel]] = ScribeInput
    output_model: ClassVar[type[BaseModel]] = ScribeOutput
    timeout_s: ClassVar[float] = 20.0
    start_line: ClassVar[str] = "file_read"
    done_line: ClassVar[str] = "file_write"

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Dispatch to the requested operation."""
        inp: ScribeInput = args
        root = ctx.settings.workspace_dir
        root.mkdir(parents=True, exist_ok=True)
        handler = {
            "read": self._read,
            "write": self._write,
            "append": self._append,
            "list": self._list,
            "exists": self._exists,
            "stat": self._stat,
            "mkdir": self._mkdir,
            "compile_check": self._compile_check,
            "analyze": self._analyze,
        }[inp.op]
        return await handler(inp, ctx, root)

    # -- helpers ------------------------------------------------------------------------------
    def _resolve(self, root: Path, inp: ScribeInput, *, allow_root: bool = False) -> Path:
        _check_depth(inp.path)
        return confine_path(root, inp.path, allow_root=allow_root)

    # -- operations ---------------------------------------------------------------------------
    async def _read(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        _check_extension(inp.path)
        if not path.is_file():
            raise NotFound("no such file")
        text, truncated, size = read_text_limited(path, READ_LIMIT)
        out = ScribeOutput(op="read", path=relative_to_root(root, path), content=text, size=size)
        return ToolOutput(
            data=out, truncated=truncated, taints_turn=True, sources=[f"workspace:{out.path}"]
        )

    async def _write(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        return await self._store(inp, ctx, root, append=False)

    async def _append(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        return await self._store(inp, ctx, root, append=True)

    async def _store(
        self, inp: ScribeInput, ctx: ToolContext, root: Path, *, append: bool
    ) -> ToolOutput:
        assert inp.content is not None
        path = self._resolve(root, inp)
        ext = _check_extension(inp.path)
        if ctx.services is None:
            raise Conflict("file store is not available")
        new_bytes = inp.content.encode("utf-8")
        if path.is_dir():
            raise Conflict("a directory exists at that path")
        exists = path.is_file()
        if exists and not append and not inp.overwrite:
            raise Conflict("file exists; pass overwrite=true to create a new version")
        if append and exists:
            if path.stat().st_size > WRITE_LIMIT:
                raise LimitExceeded("existing file is too large to append to")
            existing = path.read_bytes()
            payload = existing + new_bytes
        else:
            payload = new_bytes
        if len(payload) > WRITE_LIMIT:
            raise LimitExceeded("write exceeds the 2 MB limit")
        if b"\x00" in payload:
            raise ValidationFailure("content contains NUL bytes")
        rel = relative_to_root(root, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sources = ["operator"] if not ctx.tainted else ["operator", "untrusted_turn"]
        record = await ctx.services.files.write_version(
            logical_name=rel,
            data=payload,
            mime=TEXT_MIME_BY_EXT.get(ext, "text/plain"),
            created_by="scribe",
            chat_id=ctx.chat_id,
            sources=sources,
            external_content=ctx.tainted,
            max_bytes=WRITE_LIMIT,
            change_note="appended" if append else ("overwritten" if exists else "created"),
        )
        out = ScribeOutput(
            op="append" if append else "write",
            path=rel,
            bytes_written=len(new_bytes),
            version=record.version,
            file_id=record.file_id,
            size=len(payload),
        )
        return ToolOutput(data=out, files=[record.to_ref()], taints_turn=False)

    async def _list(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp, allow_root=True) if inp.path else root.resolve()
        if not path.is_dir():
            raise NotFound("no such directory")
        entries: list[Entry] = []
        truncated = False
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if len(entries) >= LIST_LIMIT:
                truncated = True
                break
            if child.name.startswith(".tmp-"):
                continue
            try:
                st = child.lstat()
            except OSError:
                continue
            kind = _kind(child)
            entries.append(
                Entry(
                    name=child.name,
                    kind=kind,
                    size=st.st_size if kind == "file" else None,
                    modified_epoch=st.st_mtime,
                )
            )
        rel = relative_to_root(root, path)
        return ToolOutput(
            data=ScribeOutput(op="list", path=rel, entries=entries), truncated=truncated
        )

    async def _exists(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        return ToolOutput(data=ScribeOutput(op="exists", path=inp.path, exists=path.exists()))

    async def _stat(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        if not path.exists():
            raise NotFound("no such file or directory")
        st = path.stat()
        return ToolOutput(
            data=ScribeOutput(
                op="stat",
                path=relative_to_root(root, path),
                exists=True,
                kind=_kind(path),
                size=st.st_size,
                modified_epoch=st.st_mtime,
            )
        )

    async def _mkdir(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        if path.is_file():
            raise Conflict("a file exists at that path")
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        return ToolOutput(
            data=ScribeOutput(op="mkdir", path=relative_to_root(root, path), created_dir=created)
        )

    async def _compile_check(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        ext = _check_extension(inp.path)
        if not path.is_file():
            raise NotFound("no such file")
        text, truncated, _ = read_text_limited(path, READ_LIMIT)
        if truncated:
            raise LimitExceeded("file too large to check")
        return ToolOutput(
            data=ScribeOutput(
                op="compile_check",
                path=relative_to_root(root, path),
                check=compile_check(ext, text),
            ),
            taints_turn=True,
        )

    async def _analyze(self, inp: ScribeInput, ctx: ToolContext, root: Path) -> ToolOutput:
        path = self._resolve(root, inp)
        ext = _check_extension(inp.path)
        if not path.is_file():
            raise NotFound("no such file")
        text, truncated, size = read_text_limited(path, READ_LIMIT)
        return ToolOutput(
            data=ScribeOutput(
                op="analyze",
                path=relative_to_root(root, path),
                size=size,
                analysis=analyze_text(ext, text),
            ),
            truncated=truncated,
            taints_turn=True,
        )
