"""Tool 3: ``host_telemetry_monitor`` (spec 11.4). Read-only host diagnostics via ``psutil``.

No command lines and no environment variables are ever collected. Process names are sanitised
and capped because a process name is attacker-controllable text.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar, Literal

import psutil
from pydantic import BaseModel, Field

from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput
from src.tools.security import is_control_or_format

Section = Literal["cpu", "memory", "disk", "network", "processes", "uptime"]
ALL_SECTIONS: list[Section] = ["cpu", "memory", "disk", "network", "processes", "uptime"]
SAMPLE_INTERVAL_S = 0.25


class TelemetryInput(StrictModel):
    """Which parts of the host to inspect."""

    sections: list[Section] = Field(default_factory=lambda: list(ALL_SECTIONS), max_length=6)
    top_n: int = Field(5, ge=1, le=15, description="How many top processes to list.")
    sort_by: Literal["cpu", "memory"] = "cpu"


class CpuInfo(BaseModel):
    """CPU utilisation."""

    percent_total: float
    percent_per_core: list[float]
    logical_cores: int
    physical_cores: int | None = None
    freq_mhz: float | None = None
    load_avg: list[float] | None = None


class MemoryInfo(BaseModel):
    """Memory usage in bytes."""

    total: int
    available: int
    used: int
    percent: float
    swap_total: int
    swap_used: int
    swap_percent: float


class DiskInfo(BaseModel):
    """One mounted filesystem."""

    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


class NetworkInfo(BaseModel):
    """Aggregate network counters."""

    bytes_sent: int
    bytes_recv: int
    packets_sent: int
    packets_recv: int
    errin: int
    errout: int


class ProcessInfo(BaseModel):
    """A top process (no command line, no environment)."""

    pid: int
    name: str
    cpu_percent: float
    memory_percent: float


class TelemetryOutput(BaseModel):
    """Host snapshot; sections not requested are ``None``."""

    cpu: CpuInfo | None = None
    memory: MemoryInfo | None = None
    disks: list[DiskInfo] | None = None
    network: NetworkInfo | None = None
    top_processes: list[ProcessInfo] | None = None
    uptime_s: float | None = None
    boot_time_epoch: float | None = None
    skipped: list[str] = []


def _clean_name(name: str) -> str:
    cleaned = "".join(ch for ch in name if not is_control_or_format(ch))
    return cleaned[:48]


def collect(args: TelemetryInput) -> TelemetryOutput:
    """Blocking collection; run via ``asyncio.to_thread``."""
    wanted = set(args.sections)
    skipped: list[str] = []
    out = TelemetryOutput()

    procs: list[psutil.Process] = []
    if "processes" in wanted:
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)  # prime the per-process counter
                procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    if "cpu" in wanted or "processes" in wanted:
        per_core = psutil.cpu_percent(interval=SAMPLE_INTERVAL_S, percpu=True)
    else:
        per_core = []

    if "cpu" in wanted:
        freq = None
        try:
            f = psutil.cpu_freq()
            freq = round(f.current, 1) if f else None
        except (OSError, NotImplementedError, AttributeError):
            freq = None
        try:
            load = [round(x, 2) for x in psutil.getloadavg()]
        except (OSError, AttributeError):
            load = None
        out.cpu = CpuInfo(
            percent_total=round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
            percent_per_core=[round(x, 1) for x in per_core],
            logical_cores=psutil.cpu_count(logical=True) or len(per_core),
            physical_cores=psutil.cpu_count(logical=False),
            freq_mhz=freq,
            load_avg=load,
        )
    if "memory" in wanted:
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap = (sw.total, sw.used, sw.percent)
        except (OSError, RuntimeError):
            swap = (0, 0, 0.0)
            skipped.append("swap")
        out.memory = MemoryInfo(
            total=vm.total,
            available=vm.available,
            used=vm.used,
            percent=vm.percent,
            swap_total=swap[0],
            swap_used=swap[1],
            swap_percent=swap[2],
        )
    if "disk" in wanted:
        disks: list[DiskInfo] = []
        for part in psutil.disk_partitions(all=False)[:12]:
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                skipped.append(f"disk:{part.mountpoint}")
                continue
            disks.append(
                DiskInfo(
                    mountpoint=part.mountpoint,
                    fstype=part.fstype,
                    total=usage.total,
                    used=usage.used,
                    free=usage.free,
                    percent=usage.percent,
                )
            )
        out.disks = disks
    if "network" in wanted:
        net = psutil.net_io_counters()
        out.network = NetworkInfo(
            bytes_sent=net.bytes_sent,
            bytes_recv=net.bytes_recv,
            packets_sent=net.packets_sent,
            packets_recv=net.packets_recv,
            errin=net.errin,
            errout=net.errout,
        )
    if "processes" in wanted:
        rows: list[ProcessInfo] = []
        for proc in procs:
            try:
                with proc.oneshot():
                    rows.append(
                        ProcessInfo(
                            pid=proc.pid,
                            name=_clean_name(proc.name()),
                            cpu_percent=round(proc.cpu_percent(None), 1),
                            memory_percent=round(proc.memory_percent(), 2),
                        )
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        key = "cpu_percent" if args.sort_by == "cpu" else "memory_percent"
        rows.sort(key=lambda r: getattr(r, key), reverse=True)
        out.top_processes = rows[: args.top_n]
    if "uptime" in wanted:
        boot = psutil.boot_time()
        out.boot_time_epoch = boot
        out.uptime_s = round(time.time() - boot, 1)
    out.skipped = skipped
    return out


class HostTelemetryMonitor(Tool):
    """Read-only host diagnostics."""

    name: ClassVar[str] = "host_telemetry_monitor"
    description: ClassVar[str] = (
        "Read-only snapshot of the machine Ultron lives on: CPU, memory, disks, network "
        "counters, the busiest processes, and uptime. Use it for questions about the host's "
        "current state. Do not use it for files (file_system_scribe) or remote hosts "
        '(network_ping_diagnostic). Example: {"sections": ["cpu", "memory"]}.'
    )
    input_model: ClassVar[type[StrictModel]] = TelemetryInput
    output_model: ClassVar[type[BaseModel]] = TelemetryOutput
    timeout_s: ClassVar[float] = 15.0
    start_line: ClassVar[str] = "telemetry"
    done_line: ClassVar[str] = "telemetry"

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Collect a snapshot in a worker thread."""
        snapshot = await asyncio.to_thread(collect, args)
        return ToolOutput(data=snapshot)
