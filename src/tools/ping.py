"""Tool 5: ``network_ping_diagnostic`` (spec 11.6). An egress tool; never invokes a shell.

* ICMP uses ``create_subprocess_exec`` with an argument list (host is validated first).
* TCP uses ``asyncio.open_connection``; DNS uses ``getaddrinfo``.
* The host is resolved *once* and the resolved address is what gets contacted, so a rebinding
  answer between check and use cannot redirect the probe.
* Only the summary lines of ICMP output are returned. Per-packet lines include reverse-DNS names,
  which are attacker-controlled text.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import socket
import sys
import time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from src.core.errors import PolicyDenied, ProviderUnavailable
from src.tools.base import StrictModel, Tool, ToolContext, ToolOutput, truncate_text
from src.tools.security import is_private_address, validate_host

RAW_CAP = 2000
TcpMode = Literal["icmp", "tcp", "dns"]


class PingInput(StrictModel):
    """Probe a host."""

    host: str = Field(..., min_length=1, max_length=253, description="Hostname or IP address.")
    mode: TcpMode = "icmp"
    count: int = Field(4, ge=1, le=10)
    port: int | None = Field(None, ge=1, le=65535, description="Required for mode=tcp.")
    timeout_s: float = Field(3.0, ge=1, le=10)

    @model_validator(mode="after")
    def _port(self) -> PingInput:
        if self.mode == "tcp" and self.port is None:
            raise ValueError("'port' is required for mode=tcp")
        return self


class PingOutput(BaseModel):
    """Probe statistics."""

    mode: str
    host: str
    resolved_ips: list[str] = []
    sent: int = 0
    received: int = 0
    loss_percent: float = 100.0
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    raw: str = ""


_LINUX_STATS = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets )?received", re.I)
_UNIX_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)")
_WIN_STATS = re.compile(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+)", re.I)
_WIN_RTT = re.compile(
    r"Minimum\s*=\s*(\d+)ms,\s*Maximum\s*=\s*(\d+)ms,\s*Average\s*=\s*(\d+)ms", re.I
)


def parse_ping_output(text: str) -> dict[str, Any]:
    """Parse Linux, macOS, or Windows ping summaries into numbers plus the two summary lines."""
    sent = received = 0
    rtt: tuple[float, float, float] | None = None
    summary: list[str] = []
    for line in text.splitlines():
        if (m := _LINUX_STATS.search(line)) or (m := _WIN_STATS.search(line)):
            sent, received = int(m.group(1)), int(m.group(2))
            summary.append(line.strip())
        elif m := _UNIX_RTT.search(line):
            if "min/avg/max" in line or "round-trip" in line:
                rtt = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                summary.append(line.strip())
        elif m := _WIN_RTT.search(line):
            rtt = (float(m.group(1)), float(m.group(3)), float(m.group(2)))
            summary.append(line.strip())
    return {"sent": sent, "received": received, "rtt": rtt, "summary": summary}


def _loss(sent: int, received: int) -> float:
    return round(100.0 * (sent - received) / sent, 1) if sent else 100.0


async def resolve_host(host: str, timeout: float) -> list[str]:
    """Resolve ``host`` to unique IP strings (IP literals pass through)."""
    try:
        socket.inet_pton(socket.AF_INET, host)
        return [host]
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return [host]
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout
        )
    except socket.gaierror as exc:
        raise ProviderUnavailable(f"DNS lookup failed: {exc.strerror or exc}") from exc
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


class NetworkPingDiagnostic(Tool):
    """Probe reachability of a host (egress)."""

    name: ClassVar[str] = "network_ping_diagnostic"
    description: ClassVar[str] = (
        "Check whether a host is reachable: ICMP ping, a TCP connect to a port, or a DNS lookup. "
        "Sends traffic to the named host, so only use it for hosts the operator asked about. "
        'Examples: {"host": "example.com", "mode": "icmp", "count": 4}; '
        '{"host": "192.168.1.1", "mode": "tcp", "port": 443}.'
    )
    input_model: ClassVar[type[StrictModel]] = PingInput
    output_model: ClassVar[type[BaseModel]] = PingOutput
    is_egress: ClassVar[bool] = True
    taints_turn: ClassVar[bool] = False
    timeout_s: ClassVar[float] = 40.0
    start_line: ClassVar[str] = "ping"
    done_line: ClassVar[str] = "ping"

    def effective_timeout(self, args: Any) -> float:
        """Budget scales with probe count and per-probe timeout."""
        return float(min(45.0, args.count * args.timeout_s + args.timeout_s + 3))

    def outbound_strings(self, args: Any) -> list[tuple[str, str]]:
        """The host leaves the machine (DNS query or packet destination)."""
        return [("host", args.host)]

    async def cleanup(self, ctx: ToolContext) -> None:
        """Kill a lingering ping process (timeout or cancellation)."""
        proc: asyncio.subprocess.Process | None = ctx.scratch.pop("proc", None)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def run(self, args: Any, ctx: ToolContext) -> ToolOutput:
        """Validate the host, resolve it, apply range policy, and probe."""
        inp: PingInput = args
        settings = ctx.settings
        validated = validate_host(inp.host, max_len=settings.ping_max_hostname_len)
        typed_by_operator = inp.host.lower() in ctx.operator_text.lower()
        if validated.suspicious and not typed_by_operator:
            raise PolicyDenied(
                "host looks like encoded data (long or high-entropy name); "
                "I only probe such names when the operator typed them"
            )
        ips = await resolve_host(validated.host, inp.timeout_s)
        if not ips:
            raise ProviderUnavailable("host did not resolve")
        if not settings.allow_private_ranges:
            private = [ip for ip in ips if is_private_address(ip)]
            if private:
                raise PolicyDenied("private, loopback, and link-local targets are disabled")
        target = ips[0]
        if inp.mode == "dns":
            out = PingOutput(
                mode="dns",
                host=validated.host,
                resolved_ips=ips,
                sent=1,
                received=1,
                loss_percent=0.0,
                raw="",
            )
        elif inp.mode == "tcp":
            assert inp.port is not None
            out = await self._tcp(validated.host, target, ips, inp)
        else:
            out = await self._icmp(validated.host, target, ips, inp, ctx)
        return ToolOutput(data=out)

    async def _tcp(self, host: str, target: str, ips: list[str], inp: PingInput) -> PingOutput:
        assert inp.port is not None
        rtts: list[float] = []
        for _ in range(inp.count):
            started = time.perf_counter()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(target, inp.port), timeout=inp.timeout_s
                )
            except (TimeoutError, OSError):
                continue
            rtts.append((time.perf_counter() - started) * 1000)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return PingOutput(
            mode="tcp",
            host=host,
            resolved_ips=ips,
            sent=inp.count,
            received=len(rtts),
            loss_percent=_loss(inp.count, len(rtts)),
            rtt_min_ms=round(min(rtts), 2) if rtts else None,
            rtt_avg_ms=round(sum(rtts) / len(rtts), 2) if rtts else None,
            rtt_max_ms=round(max(rtts), 2) if rtts else None,
            raw=f"tcp connect to port {inp.port}",
        )

    async def _icmp(
        self, host: str, target: str, ips: list[str], inp: PingInput, ctx: ToolContext
    ) -> PingOutput:
        binary = shutil.which("ping")
        if binary is None:
            raise ProviderUnavailable("no ping binary on this host; try mode=tcp or mode=dns")
        if sys.platform.startswith("win"):
            argv = [binary, "-n", str(inp.count), "-w", str(int(inp.timeout_s * 1000)), target]
        elif sys.platform == "darwin":
            argv = [binary, "-c", str(inp.count), "-W", str(int(inp.timeout_s * 1000)), target]
        else:
            argv = [binary, "-c", str(inp.count), "-W", str(max(1, int(inp.timeout_s))), target]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        ctx.scratch["proc"] = proc
        stdout, stderr = await proc.communicate()
        ctx.scratch.pop("proc", None)
        text = stdout.decode("utf-8", errors="replace")
        parsed = parse_ping_output(text)
        if parsed["sent"] == 0:
            reason = stderr.decode("utf-8", errors="replace").strip() or "ping produced no summary"
            raise ProviderUnavailable(f"ICMP probe failed: {truncate_text(reason, 200)[0]}")
        rtt = parsed["rtt"]
        raw, _ = truncate_text("\n".join(parsed["summary"]), RAW_CAP)
        return PingOutput(
            mode="icmp",
            host=host,
            resolved_ips=ips,
            sent=parsed["sent"],
            received=parsed["received"],
            loss_percent=_loss(parsed["sent"], parsed["received"]),
            rtt_min_ms=rtt[0] if rtt else None,
            rtt_avg_ms=rtt[1] if rtt else None,
            rtt_max_ms=rtt[2] if rtt else None,
            raw=raw,
        )
