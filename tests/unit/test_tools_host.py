"""Telemetry, scribe, ping, and the versioned file store."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from src.core.services import Services
from src.tools.base import ToolContext
from src.tools.ping import NetworkPingDiagnostic, parse_ping_output
from src.tools.scribe import FileSystemScribe
from src.tools.telemetry import HostTelemetryMonitor


# ------------------------------------------------------------------ telemetry ---------------
async def test_telemetry_snapshot(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    res = await HostTelemetryMonitor().execute({"top_n": 3}, ctx_factory())
    assert res.ok, res.error
    data = res.data
    assert data["cpu"]["logical_cores"] >= 1
    assert 0 <= data["memory"]["percent"] <= 100
    assert len(data["top_processes"]) <= 3
    assert data["uptime_s"] > 0
    assert not res.taints_turn
    for proc in data["top_processes"]:
        assert set(proc) == {"pid", "name", "cpu_percent", "memory_percent"}  # no cmdline/env


async def test_telemetry_sections_and_validation(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    ok = await HostTelemetryMonitor().execute({"sections": ["memory"]}, ctx_factory())
    assert ok.data["cpu"] is None
    assert ok.data["memory"] is not None
    bad = await HostTelemetryMonitor().execute({"sections": ["secrets"]}, ctx_factory())
    assert not bad.ok
    assert bad.error.kind == "validation"
    extra = await HostTelemetryMonitor().execute({"nope": 1}, ctx_factory())
    assert extra.error.kind == "validation"


# ------------------------------------------------------------------ scribe -------------------
async def run(tool, args, ctx):  # type: ignore[no-untyped-def]
    return await tool.execute(args, ctx)


async def test_scribe_write_read_versions(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    tool = FileSystemScribe()
    w1 = await run(tool, {"op": "write", "path": "notes/a.md", "content": "one"}, ctx_factory())
    assert w1.ok, w1.error
    assert w1.data["version"] == 1
    assert w1.files[0].logical_name == "notes/a.md"
    # existing file without overwrite -> conflict
    dup = await run(tool, {"op": "write", "path": "notes/a.md", "content": "two"}, ctx_factory())
    assert not dup.ok
    assert dup.error.kind == "conflict"
    w2 = await run(
        tool,
        {"op": "write", "path": "notes/a.md", "content": "two", "overwrite": True},
        ctx_factory(),
    )
    assert w2.data["version"] == 2
    head = services.settings.workspace_dir / "notes" / "a.md"
    assert head.read_text() == "two"
    # previous bytes are kept
    assert await services.files.read_version_bytes(w1.files[0].file_id, 1) == b"one"
    rd = await run(tool, {"op": "read", "path": "notes/a.md"}, ctx_factory())
    assert rd.data["content"] == "two"
    assert rd.taints_turn  # reading files taints the turn
    ap = await run(tool, {"op": "append", "path": "notes/a.md", "content": "+more"}, ctx_factory())
    assert ap.data["version"] == 3
    assert head.read_text() == "two+more"


async def test_scribe_snapshots_operator_file_before_overwrite(
    ctx_factory, services: Services
) -> None:  # type: ignore[no-untyped-def]
    ws = services.settings.workspace_dir
    (ws / "mine.txt").write_text("operator original")
    tool = FileSystemScribe()
    res = await run(
        tool,
        {"op": "write", "path": "mine.txt", "content": "new", "overwrite": True},
        ctx_factory(),
    )
    assert res.ok
    assert res.data["version"] == 2
    fid = res.files[0].file_id
    assert await services.files.read_version_bytes(fid, 1) == b"operator original"
    versions = await services.files.list_versions(fid)
    assert versions[0].created_by == "operator"


async def test_scribe_detects_outside_edit(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    tool = FileSystemScribe()
    await run(tool, {"op": "write", "path": "x.md", "content": "v1"}, ctx_factory())
    (services.settings.workspace_dir / "x.md").write_text("edited outside")
    res = await run(
        tool, {"op": "write", "path": "x.md", "content": "v2", "overwrite": True}, ctx_factory()
    )
    fid = res.files[0].file_id
    assert res.data["version"] == 3
    assert await services.files.read_version_bytes(fid, 2) == b"edited outside"


async def test_scribe_policy_errors(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    tool = FileSystemScribe()
    for path in ["../x.txt", "/etc/passwd", "a/../../x.txt", "x.exe", "noext", ".env"]:
        res = await run(tool, {"op": "read", "path": path}, ctx_factory())
        assert not res.ok, path
        assert res.error.kind in {"policy", "not_found"}, (path, res.error)
    big = await run(
        tool,
        {"op": "write", "path": "big.txt", "content": "x" * (2 * 1024 * 1024 + 1)},
        ctx_factory(),
    )
    assert big.error.kind == "limit"
    missing = await run(tool, {"op": "write", "path": "a.txt"}, ctx_factory())
    assert missing.error.kind == "validation"
    stray = await run(tool, {"op": "read", "path": "a.txt", "content": "x"}, ctx_factory())
    assert stray.error.kind == "validation"
    nul = await run(tool, {"op": "write", "path": "n.txt", "content": "a\x00b"}, ctx_factory())
    assert nul.error.kind == "validation"


async def test_scribe_symlink_escape(ctx_factory, services: Services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    ws = services.settings.workspace_dir
    (ws / "link.txt").symlink_to(outside)
    res = await run(FileSystemScribe(), {"op": "read", "path": "link.txt"}, ctx_factory())
    assert res.error.kind == "policy"
    w = await run(
        FileSystemScribe(),
        {"op": "write", "path": "link.txt", "content": "x", "overwrite": True},
        ctx_factory(),
    )
    assert w.error.kind == "policy"
    assert outside.read_text() == "secret"


async def test_scribe_list_stat_exists_mkdir(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    tool = FileSystemScribe()
    assert (await run(tool, {"op": "mkdir", "path": "d/e"}, ctx_factory())).data[
        "created_dir"
    ] is True
    assert (await run(tool, {"op": "mkdir", "path": "d/e"}, ctx_factory())).data[
        "created_dir"
    ] is False
    await run(tool, {"op": "write", "path": "d/f.txt", "content": "hello"}, ctx_factory())
    ls = await run(tool, {"op": "list", "path": "d"}, ctx_factory())
    assert {e["name"] for e in ls.data["entries"]} == {"e", "f.txt"}
    root_ls = await run(tool, {"op": "list"}, ctx_factory())
    assert root_ls.ok
    assert (await run(tool, {"op": "exists", "path": "d/f.txt"}, ctx_factory())).data[
        "exists"
    ] is True
    assert (await run(tool, {"op": "exists", "path": "nope.txt"}, ctx_factory())).data[
        "exists"
    ] is False
    st_ = await run(tool, {"op": "stat", "path": "d/f.txt"}, ctx_factory())
    assert st_.data["size"] == 5
    assert st_.data["kind"] == "file"
    assert (
        await run(tool, {"op": "stat", "path": "zzz.txt"}, ctx_factory())
    ).error.kind == "not_found"


async def test_scribe_list_truncates(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    ws = services.settings.workspace_dir
    (ws / "many").mkdir()
    for i in range(205):
        (ws / "many" / f"f{i:03}.txt").write_text("x")
    res = await run(FileSystemScribe(), {"op": "list", "path": "many"}, ctx_factory())
    assert len(res.data["entries"]) == 200
    assert res.truncated


async def test_scribe_compile_and_analyze(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    ws = services.settings.workspace_dir
    (ws / "good.py").write_text(
        '"""m."""\nimport os\n\n\ndef f(a):\n    """d."""\n    return a\n\n\nclass C:\n    pass\n'
    )
    (ws / "bad.py").write_text("def f(:\n  pass\n")
    (ws / "d.json").write_text('{"a": {"b": [1, 2]}}')
    (ws / "bad.json").write_text("{nope")
    (ws / "c.yaml").write_text("a: [1,")
    (ws / "t.toml").write_text('a = "x"\n')
    (ws / "q.sql").write_text("select 1")
    tool = FileSystemScribe()
    assert (await run(tool, {"op": "compile_check", "path": "good.py"}, ctx_factory())).data[
        "check"
    ]["ok"]
    bad = (await run(tool, {"op": "compile_check", "path": "bad.py"}, ctx_factory())).data["check"]
    assert bad["ok"] is False
    assert bad["line"] == 1
    assert (await run(tool, {"op": "compile_check", "path": "d.json"}, ctx_factory())).data[
        "check"
    ]["ok"]
    assert not (await run(tool, {"op": "compile_check", "path": "bad.json"}, ctx_factory())).data[
        "check"
    ]["ok"]
    assert not (await run(tool, {"op": "compile_check", "path": "c.yaml"}, ctx_factory())).data[
        "check"
    ]["ok"]
    assert (await run(tool, {"op": "compile_check", "path": "t.toml"}, ctx_factory())).data[
        "check"
    ]["ok"]
    assert (await run(tool, {"op": "compile_check", "path": "q.sql"}, ctx_factory())).data["check"][
        "supported"
    ] is False
    an = (await run(tool, {"op": "analyze", "path": "good.py"}, ctx_factory())).data["analysis"]
    assert an["python"]["functions"] == 1
    assert an["python"]["classes"] == 1
    assert "os" in an["python"]["imports"]
    js = (await run(tool, {"op": "analyze", "path": "d.json"}, ctx_factory())).data["analysis"][
        "json"
    ]
    assert js["depth"] == 3


async def test_scribe_never_executes_code(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    ws = services.settings.workspace_dir
    marker = ws / "pwned.txt"
    (ws / "evil.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    tool = FileSystemScribe()
    await run(tool, {"op": "compile_check", "path": "evil.py"}, ctx_factory())
    await run(tool, {"op": "analyze", "path": "evil.py"}, ctx_factory())
    assert not marker.exists()


@given(st.text(max_size=60))
@hsettings(
    max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
async def test_scribe_hypothesis_paths_stay_inside(
    ctx_factory, services: Services, path: str
) -> None:  # type: ignore[no-untyped-def]
    res = await run(
        FileSystemScribe(),
        {"op": "write", "path": path + ".txt", "content": "x", "overwrite": True},
        ctx_factory(),
    )
    if res.ok:
        written = (services.settings.workspace_dir / res.data["path"]).resolve()
        assert written.is_relative_to(services.settings.workspace_dir.resolve())


async def test_scribe_read_binary_rejected(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    (services.settings.workspace_dir / "b.txt").write_bytes(b"ab\x00cd")
    res = await run(FileSystemScribe(), {"op": "read", "path": "b.txt"}, ctx_factory())
    assert res.error.kind == "validation"


async def test_scribe_read_truncates(ctx_factory, services: Services) -> None:  # type: ignore[no-untyped-def]
    (services.settings.workspace_dir / "huge.txt").write_text("a" * (1024 * 1024 + 50))
    res = await run(FileSystemScribe(), {"op": "read", "path": "huge.txt"}, ctx_factory())
    assert res.ok
    assert res.truncated
    assert len(res.data["content"]) == 1024 * 1024


# ------------------------------------------------------------------ ping ---------------------
LINUX_OUT = """PING example.com (93.184.216.34) 56(84) bytes of data.
64 bytes from lga34s32-in-f14.1e100.net (93.184.216.34): icmp_seq=1 ttl=56 time=11.2 ms

--- example.com ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 3004ms
rtt min/avg/max/mdev = 11.123/12.456/14.789/1.234 ms
"""
MAC_OUT = """--- 8.8.8.8 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 10.1/11.2/12.3/0.5 ms
"""
WIN_OUT = """Ping statistics for 8.8.8.8:
    Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 9ms, Maximum = 12ms, Average = 10ms
"""


def test_parse_ping_outputs() -> None:
    lin = parse_ping_output(LINUX_OUT)
    assert (lin["sent"], lin["received"]) == (4, 3)
    assert lin["rtt"] == (11.123, 12.456, 14.789)
    assert all("lga34s32" not in line for line in lin["summary"])  # no per-packet / PTR text
    mac = parse_ping_output(MAC_OUT)
    assert (mac["sent"], mac["received"]) == (3, 3)
    win = parse_ping_output(WIN_OUT)
    assert win["rtt"] == (9.0, 10.0, 12.0)
    assert parse_ping_output("garbage")["sent"] == 0


async def test_ping_validation_blocks_injection(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    tool = NetworkPingDiagnostic()
    for host in ["-c 1000 8.8.8.8", "x; id", "$(id)", "a b", "-h"]:
        res = await tool.execute({"host": host}, ctx_factory())
        assert not res.ok
        assert res.error.kind == "validation", host
    assert (
        await tool.execute({"host": "localhost", "mode": "tcp"}, ctx_factory())
    ).error.kind == "validation"
    assert (
        await tool.execute({"host": "localhost", "count": 99}, ctx_factory())
    ).error.kind == "validation"


async def test_ping_tcp_and_dns_local(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        tool = NetworkPingDiagnostic()
        res = await tool.execute(
            {"host": "127.0.0.1", "mode": "tcp", "port": port, "count": 3}, ctx_factory()
        )
        assert res.ok, res.error
        assert res.data["received"] == 3
        assert res.data["loss_percent"] == 0
        assert res.data["rtt_min_ms"] is not None
    closed = await tool.execute(
        {"host": "127.0.0.1", "mode": "tcp", "port": port, "count": 2, "timeout_s": 1},
        ctx_factory(),
    )
    assert closed.ok
    assert closed.data["received"] == 0
    assert closed.data["loss_percent"] == 100
    dns = await tool.execute({"host": "localhost", "mode": "dns"}, ctx_factory())
    assert dns.ok
    assert dns.data["resolved_ips"]


async def test_ping_private_ranges_policy(settings, ctx_factory) -> None:  # type: ignore[no-untyped-def]
    settings.allow_private_ranges = False
    res = await NetworkPingDiagnostic().execute({"host": "127.0.0.1", "mode": "dns"}, ctx_factory())
    assert res.error.kind == "policy"


async def test_ping_exfil_host_requires_operator_typing(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    host = "a3f9c2e7b1d4a8f6c0e5b7.invalid"
    tool = NetworkPingDiagnostic()
    denied = await tool.execute(
        {"host": host, "mode": "dns"}, ctx_factory(operator_text="ping something")
    )
    assert denied.error.kind == "policy"
    typed = await tool.execute(
        {"host": host, "mode": "dns"}, ctx_factory(operator_text=f"please ping {host}")
    )
    assert typed.error.kind != "policy"  # passes the heuristic (then fails DNS, which is fine)


@pytest.mark.skipif(shutil.which("ping") is None, reason="no ping binary")
async def test_ping_icmp_graceful(ctx_factory) -> None:  # type: ignore[no-untyped-def]
    res = await NetworkPingDiagnostic().execute(
        {"host": "127.0.0.1", "count": 1, "timeout_s": 1}, ctx_factory()
    )
    # Either it works, or the sandbox lacks CAP_NET_RAW and we get a typed error, never a raise.
    assert res.ok or res.error.kind == "unavailable"


async def test_tool_context_without_services_is_safe(settings) -> None:  # type: ignore[no-untyped-def]
    res = await FileSystemScribe().execute(
        {"op": "write", "path": "a.txt", "content": "x"}, ToolContext(settings=settings)
    )
    assert res.error.kind == "conflict"
