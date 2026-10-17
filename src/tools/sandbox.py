"""Tool 2: ``isolated_python_sandbox`` (spec 11.3).

Runs model-written Python in a separate interpreter (``python -I``) inside a fresh temp
directory with a scrubbed environment, a wall-clock timeout, a process-group kill, and POSIX
resource limits. A small in-process audit hook adds *defence in depth* (no process spawning, no
sockets, no reads of the application's data directories).

**This is not a hardened sandbox.** A subprocess with rlimits shares the user's filesystem and
network; the audit hook is bypassable by native code. ``docs/security-model.md`` says so, and
the policy gate treats this tool as an egress path in tainted turns for that reason.

Files: ``input_files`` are copied read-only into ``./in/``; anything the program writes to
``./out/`` is harvested, validated, scanned, and registered as a new file version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from src.core.errors import Conflict, NotFound, PolicyDenied
from src.files.types import HARVEST_EXTENSIONS, extension_of, mime_for_output
from src.files.validators import sniff_matches_extension
from src.tools.base import FileRef, StrictModel, Tool, ToolContext, ToolOutput, truncate_text
from src.tools.security import sanitize_filename

CODE_LIMIT_BYTES = 20 * 1024
STDIN_LIMIT_BYTES = 20 * 1024
STREAM_CAP = 10 * 1024
FLOOD_KILL_BYTES = 64 * 1024 * 1024
INPUT_FILE_LIMIT = 20 * 1024 * 1024
POSIX = os.name == "posix"

# The child bootstrap: limits first, audit hook second, then the user's program.
BOOTSTRAP = r"""
import json, os, sys, traceback
cfg = json.loads(sys.argv[1])
try:
    import resource
    def _lim(which, value):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass
    _lim(resource.RLIMIT_CPU, cfg["cpu_s"])
    _lim(resource.RLIMIT_AS, cfg["mem_bytes"])
    _lim(resource.RLIMIT_FSIZE, cfg["fsize_bytes"])
    _lim(resource.RLIMIT_NOFILE, cfg["nofile"])
    _lim(resource.RLIMIT_CORE, 0)
    if hasattr(resource, "RLIMIT_NPROC") and cfg.get("nproc"):
        _lim(resource.RLIMIT_NPROC, cfg["nproc"])
except ImportError:
    pass

_DENY = tuple(os.path.realpath(p) for p in cfg["deny_paths"])
_BLOCK = {
    "subprocess.Popen", "os.system", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn",
    "os.spawn", "ctypes.dlopen", "ctypes.dlsym",
}
_NET = {"socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo",
        "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendmsg"}

def _hook(event, args):
    if event in _BLOCK:
        raise PermissionError("sandbox: process creation and native loading are disabled")
    if cfg["block_network"] and event in _NET:
        raise PermissionError("sandbox: network access is disabled")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            real = os.path.realpath(os.fsdecode(args[0]))
        except (OSError, ValueError):
            return
        for denied in _DENY:
            if real == denied or real.startswith(denied + os.sep):
                raise PermissionError("sandbox: that location is off limits")

source = open("main.py", encoding="utf-8").read()
if "matplotlib" in source or "pyplot" in source:
    # Trusted warm-up before the hook goes in: building matplotlib's font cache can run helper
    # programs (fontconfig) that the sandbox would otherwise refuse.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import io
        import matplotlib.pyplot as _plt
        _fig = _plt.figure()
        _fig.text(0.5, 0.5, "warm")
        _fig.savefig(io.BytesIO(), format="png")
        _plt.close(_fig)
    except Exception:
        pass
sys.addaudithook(_hook)
sys.argv = ["main.py"]
sys.path.insert(0, os.getcwd())
code = compile(source, "main.py", "exec")
try:
    exec(code, {"__name__": "__main__", "__file__": "main.py"})
except SystemExit as exc:
    sys.stdout.flush()
    sys.stderr.flush()
    raise
except BaseException as exc:
    tb = exc.__traceback__.tb_next if exc.__traceback__ else None
    traceback.print_exception(type(exc), exc, tb)
    sys.stdout.flush()
    sys.exit(1)
"""


class SandboxInput(StrictModel):
    """A program to run."""

    code: str = Field(..., min_length=1, description="Python source (max 20 KB).")
    timeout_s: int = Field(10, ge=1, le=30)
    stdin: str = Field("", description="Text piped to standard input (max 20 KB).")
    input_files: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Attachment (att_...) or file (file_...) ids, readable at ./in/<name>.",
    )

    @field_validator("code")
    @classmethod
    def _code_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > CODE_LIMIT_BYTES:
            raise ValueError("code exceeds 20 KB")
        if "\x00" in value:
            raise ValueError("code contains a NUL byte")
        return value

    @field_validator("stdin")
    @classmethod
    def _stdin_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > STDIN_LIMIT_BYTES:
            raise ValueError("stdin exceeds 20 KB")
        return value


class HarvestedFile(BaseModel):
    """An output file that was kept."""

    name: str
    logical_name: str
    file_id: str
    version: int
    size: int
    mime: str
    sha256: str


class DiscardedFile(BaseModel):
    """An output file that was thrown away, with the reason."""

    name: str
    reason: str


class SandboxOutput(BaseModel):
    """What the program did."""

    stdout: str
    stderr: str
    exit_code: int | None
    signal: str | None = None
    timed_out: bool
    duration_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    input_files: dict[str, str] = {}
    harvested: list[HarvestedFile] = []
    discarded: list[DiscardedFile] = []


async def drain(
    stream: asyncio.StreamReader | None, cap: int, kill: asyncio.Event
) -> tuple[bytes, bool]:
    """Read a stream to EOF, keeping only the first ``cap`` bytes; set ``kill`` on floods."""
    if stream is None:
        return b"", False
    kept = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(kept) < cap:
            kept.extend(chunk[: cap - len(kept)])
        if total > FLOOD_KILL_BYTES:
            kill.set()
            break
    return bytes(kept), total > cap


def _task_count_for_user() -> int | None:
    """Count tasks (processes and threads) owned by this user, to size RLIMIT_NPROC."""
    try:
        import psutil

        uid = os.getuid()
        total = 0
        for proc in psutil.process_iter(["uids", "num_threads"]):
            info = proc.info
            if info.get("uids") and info["uids"].real == uid:
                total += int(info.get("num_threads") or 1)
        return total
    except Exception:  # best effort; limits are defence in depth
        return None


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if POSIX:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def scrubbed_env(workdir: Path) -> dict[str, str]:
    """Return the minimal environment for the child: no keys, no user paths."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin" if POSIX else os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "TEMP": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "random",
        "MPLCONFIGDIR": str(workdir / ".mpl"),
        "MPLBACKEND": "Agg",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {}),
    }


class IsolatedPythonSandbox(Tool):
    """Run Python in a limited subprocess."""

    name: ClassVar[str] = "isolated_python_sandbox"
    description: ClassVar[str] = (
        "Execute a short Python program and return its stdout, stderr, and exit code. Use it to "
        "test a hypothesis, compute, parse data, or draw a custom chart (save to ./out/ to keep "
        "files, e.g. plt.savefig('out/plot.png')). Attached files are readable at ./in/<name>. "
        "No network, no subprocesses, 10 s default limit, 256 MB memory. Never put secrets in "
        "code. For ordinary documents use artifact_forge. "
        'Example: {"code": "print(sum(range(10)))", "timeout_s": 5}.'
    )
    input_model: ClassVar[type[StrictModel]] = SandboxInput
    output_model: ClassVar[type[BaseModel]] = SandboxOutput
    taints_turn: ClassVar[bool] = False
    timeout_s: ClassVar[float] = 45.0
    start_line: ClassVar[str] = "sandbox_start"
    done_line: ClassVar[str] = "forge_done"
    error_line: ClassVar[str] = "sandbox_error"

    def effective_timeout(self, args: Any) -> float:
        """Program budget plus time for staging and harvesting."""
        return float(args.timeout_s) + 15.0

    def is_egress_call(self, args: Any, ctx: ToolContext) -> bool:
        """The subprocess backend cannot enforce 'no network', so it always counts as egress."""
        return True

    def outbound_strings(self, args: Any) -> list[tuple[str, str]]:
        """The code is what could carry data out; the gate shows it to the operator."""
        return [("code", args.code)]

    async def cleanup(self, ctx: ToolContext) -> None:
        """Kill the process group and delete the temp directory."""
        proc: asyncio.subprocess.Process | None = ctx.scratch.pop("proc", None)
        if proc is not None:
            _kill_group(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)
        workdir: Path | None = ctx.scratch.pop("workdir", None)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Stage inputs, run the program, harvest outputs."""
        inp: SandboxInput = args
        settings = ctx.settings
        workdir = Path(tempfile.mkdtemp(prefix="ultron-sbx-")).resolve()
        ctx.scratch["workdir"] = workdir
        (workdir / "in").mkdir()
        (workdir / "out").mkdir()
        (workdir / "main.py").write_text(inp.code, encoding="utf-8")
        (workdir / "boot.py").write_text(BOOTSTRAP, encoding="utf-8")
        input_map, input_ids = await self._stage_inputs(inp.input_files, workdir / "in", ctx)

        cfg = {
            "cpu_s": inp.timeout_s + 2,
            "mem_bytes": settings.sandbox_memory_mb * 1024 * 1024,
            "fsize_bytes": settings.sandbox_max_output_mb * 1024 * 1024,
            "nofile": 64,
            "nproc": (n + 48) if POSIX and (n := _task_count_for_user()) else 0,
            "block_network": True,
            "deny_paths": [
                str(p)
                for p in {
                    settings.data_dir,
                    settings.workspace_dir,
                    settings.attachments_dir,
                    settings.chroma_dir,
                    settings.resolve(".env"),
                }
            ],
        }
        argv = [sys.executable, "-I", str(workdir / "boot.py"), json.dumps(cfg)]
        kwargs: dict[str, Any] = (
            {"start_new_session": True}
            if POSIX
            else {"creationflags": getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)}
        )
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=scrubbed_env(workdir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        ctx.scratch["proc"] = proc
        flood = asyncio.Event()
        timed_out = False
        assert proc.stdin is not None
        proc.stdin.write(inp.stdin.encode("utf-8"))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()
        out_task = asyncio.create_task(drain(proc.stdout, STREAM_CAP, flood))
        err_task = asyncio.create_task(drain(proc.stderr, STREAM_CAP, flood))
        wait_task = asyncio.create_task(proc.wait())
        flood_task = asyncio.create_task(flood.wait())
        done, _ = await asyncio.wait(
            {wait_task, flood_task}, timeout=inp.timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_task not in done:
            timed_out = flood_task not in done
            _kill_group(proc)
            await proc.wait()
        flood_task.cancel()
        stdout_b, out_trunc = await out_task
        stderr_b, err_trunc = await err_task
        ctx.scratch.pop("proc", None)
        duration_ms = int((time.perf_counter() - started) * 1000)
        code = proc.returncode
        sig = None
        if code is not None and code < 0:
            try:
                sig = signal.Signals(-code).name
            except ValueError:
                sig = f"signal {-code}"
        harvested, discarded, refs = await self._harvest(workdir / "out", ctx, input_ids)
        stdout, t1 = truncate_text(stdout_b.decode("utf-8", errors="replace"), STREAM_CAP)
        stderr, t2 = truncate_text(stderr_b.decode("utf-8", errors="replace"), STREAM_CAP)
        out = SandboxOutput(
            stdout=stdout,
            stderr=stderr,
            exit_code=code,
            signal=sig,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout_truncated=out_trunc or t1,
            stderr_truncated=err_trunc or t2,
            input_files=input_map,
            harvested=harvested,
            discarded=discarded,
        )
        return ToolOutput(
            data=out,
            files=refs,
            truncated=out.stdout_truncated or out.stderr_truncated,
            taints_turn=bool(inp.input_files),
            sources=["sandbox:code", *input_ids],
        )

    # ------------------------------------------------------------------ staging -----------
    async def _stage_inputs(
        self, ids: list[str], in_dir: Path, ctx: ToolContext
    ) -> tuple[dict[str, str], list[str]]:
        if not ids:
            return {}, []
        if ctx.services is None:
            raise Conflict("file store is not available")
        store = ctx.services.files
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for item in ids:
            if item.startswith("att_"):
                rec = await store.get_attachment(item)
                if rec is None or (ctx.chat_id and rec.chat_id != ctx.chat_id):
                    raise NotFound(f"attachment {item} not found in this chat")
                data = store.attachment_path(rec).read_bytes()
                name = rec.safe_name
            elif item.startswith("file_"):
                latest = await store.latest_version(item)
                if latest is None:
                    raise NotFound(f"file {item} not found")
                data = await store.read_version_bytes(item, latest.version)
                name = Path(latest.logical_name).name
            else:
                raise PolicyDenied("input_files must be att_... or file_... ids")
            if len(data) > INPUT_FILE_LIMIT:
                raise PolicyDenied(f"{item} is too large to stage")
            base = sanitize_filename(name)
            candidate, n = base, 1
            while candidate in used:
                n += 1
                stem, dot, ext = base.rpartition(".")
                candidate = f"{stem}_{n}{dot}{ext}" if dot else f"{base}_{n}"
            used.add(candidate)
            target = in_dir / candidate
            target.write_bytes(data)
            target.chmod(0o444)
            mapping[item] = f"in/{candidate}"
        return mapping, list(ids)

    # ------------------------------------------------------------------ harvesting --------
    async def _harvest(
        self, out_dir: Path, ctx: ToolContext, derived_from: list[str]
    ) -> tuple[list[HarvestedFile], list[DiscardedFile], list[FileRef]]:
        harvested: list[HarvestedFile] = []
        discarded: list[DiscardedFile] = []
        refs: list[FileRef] = []
        if ctx.services is None:
            return harvested, discarded, refs
        settings = ctx.settings
        max_files = settings.sandbox_max_output_files
        budget = settings.sandbox_max_output_mb * 1024 * 1024
        scanner = ctx.services.scanner
        candidates: list[tuple[str, Path]] = []
        for root, dirs, files in os.walk(out_dir, followlinks=False):
            root_path = Path(root)
            for d in list(dirs):
                if (root_path / d).is_symlink():
                    discarded.append(
                        DiscardedFile(
                            name=str((root_path / d).relative_to(out_dir)), reason="symlink"
                        )
                    )
                    dirs.remove(d)
            for fname in sorted(files):
                path = root_path / fname
                candidates.append((path.relative_to(out_dir).as_posix(), path))
        external = ctx.tainted or bool(derived_from)
        for rel, path in sorted(candidates):
            try:
                st = path.lstat()
            except OSError:
                discarded.append(DiscardedFile(name=rel, reason="unreadable"))
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                discarded.append(DiscardedFile(name=rel, reason="not a regular file"))
                continue
            ext = extension_of(rel)
            if ext not in HARVEST_EXTENSIONS:
                discarded.append(
                    DiscardedFile(name=rel, reason=f"type '{ext or 'none'}' not allowed")
                )
                continue
            if len(harvested) >= max_files:
                discarded.append(DiscardedFile(name=rel, reason="too many output files"))
                continue
            if st.st_size > budget:
                discarded.append(DiscardedFile(name=rel, reason="exceeds the output size budget"))
                continue
            data = path.read_bytes()
            if not sniff_matches_extension(data, ext):
                discarded.append(
                    DiscardedFile(name=rel, reason="content does not match its extension")
                )
                continue
            if ext in {
                ".txt",
                ".md",
                ".csv",
                ".json",
                ".py",
                ".js",
                ".ts",
                ".sql",
                ".yaml",
                ".yml",
                ".toml",
                ".log",
                ".tsv",
                ".css",
            } and scanner.contains_secret(data.decode("utf-8", errors="replace")):
                discarded.append(DiscardedFile(name=rel, reason="contains credential-like text"))
                continue
            budget -= len(data)
            safe_rel = "/".join(sanitize_filename(part) for part in rel.split("/"))
            record = await ctx.services.files.write_version(
                logical_name=f"outputs/{safe_rel}",
                data=data,
                mime=mime_for_output(rel),
                created_by="sandbox",
                chat_id=ctx.chat_id,
                derived_from=derived_from,
                sources=["sandbox:code", *derived_from],
                external_content=external,
                change_note="written by sandbox program",
            )
            harvested.append(
                HarvestedFile(
                    name=rel,
                    logical_name=record.logical_name,
                    file_id=record.file_id,
                    version=record.version,
                    size=record.size_bytes,
                    mime=record.mime,
                    sha256=record.sha256,
                )
            )
            refs.append(record.to_ref())
        return harvested, discarded, refs
