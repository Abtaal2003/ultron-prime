"""HTTP API: authentication, CSRF, headers, routes, SSE resume (spec 6.4, 6.5)."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from src.api.app import create_app
from src.api.auth import AuthService
from src.api.export import export_schemas
from src.core.llm.fake import Reply
from src.core.wiring import Runtime

from .conftest import RoleScript

PASS = "correct horse battery"
ORIGIN = {"Origin": "http://localhost:8787"}
BASE = "http://localhost:8787"
API = "/api/v1"


@dataclass
class Api:
    """A signed-in test client."""

    client: httpx.AsyncClient
    runtime: Runtime
    csrf: str

    def h(self, **extra: str) -> dict[str, str]:
        return {**ORIGIN, "X-CSRF-Token": self.csrf, **extra}

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(API + path, **kw)

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        kw.setdefault("headers", self.h())
        return await self.client.post(API + path, **kw)

    async def patch(self, path: str, **kw: Any) -> httpx.Response:
        kw.setdefault("headers", self.h())
        return await self.client.patch(API + path, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        kw.setdefault("headers", self.h())
        return await self.client.delete(API + path, **kw)


async def _serve(runtime: Runtime, **app_kw: Any) -> AsyncIterator[httpx.AsyncClient]:
    await AuthService(runtime.db, runtime.settings)._store_hash(PASS)
    app = create_app(runtime=runtime, **app_kw)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


@pytest.fixture
async def anon(make_runtime: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime]]:
    runtime: Runtime = await make_runtime(login_max_attempts=3)
    async for client in _serve(runtime):
        yield client, runtime


@pytest.fixture
async def api(anon: tuple[httpx.AsyncClient, Runtime]) -> Api:
    client, runtime = anon
    res = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
    assert res.status_code == 200, res.text
    return Api(client, runtime, res.json()["csrf_token"])


async def sse(api: Api, turn_id: str, **headers: str) -> list[tuple[str, str, dict[str, Any]]]:
    res = await api.get(f"/turns/{turn_id}/events", headers=headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    out: list[tuple[str, str, dict[str, Any]]] = []
    for block in res.text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if "data" in fields:
            out.append((fields["id"], fields["event"], json.loads(fields["data"])))
    return out


# ------------------------------------------------------------------------------ auth ------


async def test_health_is_open_and_everything_else_needs_a_session(
    anon: tuple[httpx.AsyncClient, Runtime],
) -> None:
    client, _ = anon
    assert (await client.get(f"{API}/health")).json() == {"status": "ok"}
    for path in ("/chats", "/memory", "/settings", "/status", "/files", "/redteam/latest"):
        res = await client.get(API + path)
        assert res.status_code == 401, path
        assert res.json()["error"]["kind"] == "permission"
    session = (await client.get(f"{API}/auth/session")).json()
    assert session["authenticated"] is False


async def test_login_sets_hardened_cookies(anon: tuple[httpx.AsyncClient, Runtime]) -> None:
    client, runtime = anon
    res = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
    cookies = res.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("ultron_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    csrf_cookie = next(c for c in cookies if c.startswith("ultron_csrf="))
    assert "HttpOnly" not in csrf_cookie  # the page must be able to echo it
    body = res.json()
    assert body["authenticated"] and not body["remote"] and body["csrf_token"]
    # Only hashes are stored: the raw cookie value is not in the database.
    token = session_cookie.split(";")[0].split("=", 1)[1]
    rows = await runtime.db.fetchall("SELECT id FROM sessions")
    assert rows and all(token not in r["id"] for r in rows)
    stored = await runtime.db.fetchone("SELECT passphrase_hash FROM operator_auth")
    assert stored is not None and stored["passphrase_hash"].startswith("$argon2")
    assert PASS not in stored["passphrase_hash"]
    assert (await client.get(f"{API}/auth/session")).json()["authenticated"] is True


async def test_wrong_passphrase_is_refused_then_locked_out(
    anon: tuple[httpx.AsyncClient, Runtime],
) -> None:
    client, _ = anon
    for _ in range(3):
        res = await client.post(f"{API}/auth/login", json={"passphrase": "nope"}, headers=ORIGIN)
        assert res.status_code == 403
    locked = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
    assert locked.status_code == 429 and "Retry-After" in locked.headers


async def test_csrf_and_origin_are_enforced_on_state_changes(api: Api) -> None:
    body = {"title": "x"}
    no_csrf = await api.client.post(f"{API}/chats", json=body, headers=ORIGIN)
    assert no_csrf.status_code == 403 and "CSRF" in no_csrf.json()["error"]["message"]
    bad_csrf = await api.client.post(
        f"{API}/chats", json=body, headers={**ORIGIN, "X-CSRF-Token": "forged"}
    )
    assert bad_csrf.status_code == 403
    evil = await api.client.post(
        f"{API}/chats",
        json=body,
        headers={"Origin": "https://evil.example", "X-CSRF-Token": api.csrf},
    )
    assert evil.status_code == 403 and evil.json()["error"]["message"] == "origin not allowed"
    missing = await api.client.post(f"{API}/chats", json=body, headers={"X-CSRF-Token": api.csrf})
    assert missing.status_code == 403
    assert (await api.post("/chats", json=body)).status_code == 201


async def test_dns_rebinding_host_is_rejected(api: Api) -> None:
    res = await api.client.get(f"{API}/health", headers={"Host": "attacker.example"})
    assert res.status_code == 400


async def test_security_headers_on_every_response(api: Api) -> None:
    for res in (await api.get("/health"), await api.get("/nope"), await api.get("/chats")):
        h = res.headers
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("style-src-attr")[0]
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert "camera=(self)" in h["permissions-policy"]
    assert (await api.get("/chats")).headers["cache-control"] == "no-store"


async def test_logout_logout_all_and_passphrase_change(api: Api) -> None:
    assert (await api.get("/auth/sessions")).json()
    short = await api.post("/auth/passphrase", json={"current": PASS, "new": "short"})
    assert short.status_code == 422
    wrong = await api.post("/auth/passphrase", json={"current": "bad", "new": "a long enough one!"})
    assert wrong.status_code == 403
    ok = await api.post("/auth/passphrase", json={"current": PASS, "new": "a long enough one!"})
    assert ok.status_code == 200
    assert (await api.get("/chats")).status_code == 401  # every session was signed out
    again = await api.client.post(
        f"{API}/auth/login", json={"passphrase": "a long enough one!"}, headers=ORIGIN
    )
    assert again.status_code == 200
    csrf = again.json()["csrf_token"]
    out = await api.client.post(f"{API}/auth/logout", headers={**ORIGIN, "X-CSRF-Token": csrf})
    assert out.status_code == 200
    assert (await api.get("/chats")).status_code == 401


async def test_logout_all_revokes_other_devices(
    anon: tuple[httpx.AsyncClient, Runtime],
) -> None:
    client, runtime = anon
    first = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
    other = AuthService(runtime.db, runtime.settings)
    second = await other.login(PASS, remote_addr="100.64.0.9", user_agent="phone")
    assert second.session.is_remote
    res = await client.post(
        f"{API}/auth/logout-all",
        headers={**ORIGIN, "X-CSRF-Token": first.json()["csrf_token"]},
    )
    assert res.json() == {"revoked": 2}
    assert await other.get_session(second.token) is None


async def test_idle_and_absolute_expiry(anon: tuple[httpx.AsyncClient, Runtime]) -> None:
    _, runtime = anon
    auth = AuthService(runtime.db, runtime.settings)
    live = await auth.login(PASS, remote_addr="127.0.0.1", user_agent=None)
    assert await auth.get_session(live.token) is not None
    await runtime.db.execute("UPDATE sessions SET last_seen_at='2000-01-01T00:00:00+00:00'")
    assert await auth.get_session(live.token) is None  # idle
    fresh = await auth.login(PASS, remote_addr="127.0.0.1", user_agent=None)
    await runtime.db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    assert await auth.get_session(fresh.token) is None  # absolute
    assert await auth.get_session(None) is None and await auth.get_session("junk") is None


# ----------------------------------------------------------------------------- chats ------


async def test_chat_lifecycle_and_message_paging(api: Api, script: RoleScript) -> None:
    created = (await api.post("/chats", json={"title": "Alpha"})).json()
    chat_id = created["id"]
    assert created["private"] is False and created["active_turn"] is None
    patched = await api.patch(f"/chats/{chat_id}", json={"title": "Beta", "private": True})
    assert patched.json()["title"] == "Beta" and patched.json()["private"] is True
    await api.patch(f"/chats/{chat_id}", json={"private": False, "archived": True})
    assert chat_id not in [c["id"] for c in (await api.get("/chats")).json()]
    assert chat_id in [c["id"] for c in (await api.get("/chats", params={"archived": True})).json()]
    history = api.runtime.services.history
    assert history is not None
    ids = [await history.add_message(chat_id, "user", f"message {i}") for i in range(5)]
    page = (await api.get(f"/chats/{chat_id}/messages", params={"limit": 2})).json()
    assert [m["content"] for m in page["messages"]] == ["message 3", "message 4"]
    assert page["has_more"] is True
    older = (
        await api.get(
            f"/chats/{chat_id}/messages", params={"limit": 2, "before": page["messages"][0]["id"]}
        )
    ).json()
    assert [m["content"] for m in older["messages"]] == ["message 1", "message 2"]
    assert (
        await api.get(f"/chats/{chat_id}/messages", params={"before": "msg_x"})
    ).status_code == 404
    assert ids
    assert (await api.get("/chats/chat_missing")).status_code == 404
    assert (await api.delete(f"/chats/{chat_id}")).status_code == 204
    assert (await api.get(f"/chats/{chat_id}")).status_code == 404


# ----------------------------------------------------------------------------- turns ------


async def test_turn_streams_over_sse_and_resumes_from_last_event_id(
    api: Api, script: RoleScript
) -> None:
    script.push("main", Reply(text="Denominator guarded. Next."))
    chat_id = (await api.post("/chats", json={})).json()["id"]
    res = await api.post(f"/chats/{chat_id}/turns", data={"text": "why did line 14 crash"})
    assert res.status_code == 202
    turn_id = res.json()["turn_id"]
    events = await sse(api, turn_id)
    kinds = [e[1] for e in events]
    assert kinds[0] == "TurnStarted" and kinds[-1] == "TurnClosed" and "AgentDone" in kinds
    assert [int(e[0]) for e in events] == sorted(int(e[0]) for e in events)
    assert events[kinds.index("AgentDone")][2]["final_text"].startswith("Denominator")
    cut = events[2][0]
    resumed = await sse(api, turn_id, **{"Last-Event-ID": cut})
    assert [e[0] for e in resumed] == [e[0] for e in events if int(e[0]) > int(cut)]
    via_query = await api.get(f"/turns/{turn_id}/events", params={"after": events[-2][0]})
    assert via_query.text.count("event: TurnClosed") == 1
    assert (await api.get("/turns/turn_missing/events")).status_code == 404
    msgs = (await api.get(f"/chats/{chat_id}/messages")).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


async def test_second_turn_in_a_busy_chat_is_a_conflict(api: Api, script: RoleScript) -> None:
    script.push("main", Reply(tool_calls=[("echo", {"text": "x", "delay": 1})]), Reply(text="done"))
    chat_id = (await api.post("/chats", json={})).json()["id"]
    first = await api.post(f"/chats/{chat_id}/turns", data={"text": "one"})
    await asyncio.sleep(0.2)
    second = await api.post(f"/chats/{chat_id}/turns", data={"text": "two"})
    assert second.status_code == 409
    assert (await api.get(f"/chats/{chat_id}")).json()["active_turn"] == first.json()["turn_id"]
    stop = await api.post(f"/turns/{first.json()['turn_id']}/stop")
    assert stop.json() == {"stopped": True}
    events = await sse(api, first.json()["turn_id"])
    assert next(e for e in events if e[1] == "AgentDone")[2]["status"] == "stopped"
    assert (await api.post("/turns/turn_missing/stop")).json() == {"stopped": False}


async def test_empty_turn_is_rejected(api: Api) -> None:
    chat_id = (await api.post("/chats", json={})).json()["id"]
    res = await api.post(f"/chats/{chat_id}/turns", data={"text": "   "})
    assert res.status_code == 422
    missing = await api.post("/chats/chat_missing/turns", data={"text": "hi"})
    assert missing.status_code == 404


async def test_uploads_are_ingested_and_limits_hold(api: Api, script: RoleScript) -> None:
    chat_id = (await api.post("/chats", json={})).json()["id"]
    res = await api.post(
        f"/chats/{chat_id}/turns",
        data={"text": "read this"},
        files=[("files", ("notes.txt", b"alpha beta gamma", "text/plain"))],
    )
    assert res.status_code == 202
    att = res.json()["attachments"][0]
    assert att["name"] == "notes.txt" and att["family"] == "text"
    await sse(api, res.json()["turn_id"])
    meta = (await api.get(f"/attachments/{att['id']}")).json()
    assert meta["trust"] in {"trusted", "external"} and meta["size"] == 16
    flipped = await api.patch(f"/attachments/{att['id']}", json={"trust": "external"})
    assert flipped.json()["trust"] == "external"
    assert (await api.patch(f"/attachments/{att['id']}", json={"trust": "x"})).status_code == 403
    assert (await api.get("/attachments/att_missing")).status_code == 404

    many = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(6)]
    too_many = await api.post(f"/chats/{chat_id}/turns", data={"text": "hi"}, files=many)
    assert too_many.status_code in {413, 422}
    exe = await api.post(
        f"/chats/{chat_id}/turns",
        data={"text": "hi"},
        files=[("files", ("run.exe", b"MZ\x90\x00" + b"\x00" * 64, "application/x-msdownload"))],
    )
    assert exe.status_code == 422
    rows = await api.runtime.db.fetchall("SELECT id FROM attachments WHERE chat_id=?", (chat_id,))
    assert len(rows) == 1  # rejected uploads leave nothing behind


async def test_oversized_upload_is_cut_off(make_runtime: Any) -> None:
    runtime: Runtime = await make_runtime(upload_max_mb=1)
    async for client in _serve(runtime):
        login = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
        csrf = login.json()["csrf_token"]
        chat = (
            await client.post(f"{API}/chats", json={}, headers={**ORIGIN, "X-CSRF-Token": csrf})
        ).json()["id"]
        big = b"a" * (2 * 1024 * 1024)
        res = await client.post(
            f"{API}/chats/{chat}/turns",
            data={"text": "big"},
            files=[("files", ("big.txt", big, "text/plain"))],
            headers={**ORIGIN, "X-CSRF-Token": csrf},
        )
        assert res.status_code == 413


async def test_request_body_limit(make_runtime: Any) -> None:
    runtime: Runtime = await make_runtime(max_request_mb=1)
    async for client in _serve(runtime):
        res = await client.post(
            f"{API}/auth/login", content=b"x" * (2 * 1024 * 1024), headers=ORIGIN
        )
        assert res.status_code == 413
        assert res.json()["error"]["kind"] == "limit"


async def test_egress_decision_endpoint_validates(api: Api) -> None:
    assert (
        await api.post("/egress/call_x/decision", json={"decision": "approve"})
    ).status_code == 404
    assert (
        await api.post("/egress/call_x/decision", json={"decision": "maybe"})
    ).status_code == 403


async def test_egress_hold_can_be_approved_over_http(api: Api, script: RoleScript) -> None:
    script.push(
        "main",
        Reply(tool_calls=[("web_omniscience_search", {"query": "python release news"})]),
        Reply(
            tool_calls=[("web_omniscience_search", {"query": "attacker.io qzx9 private roadmap"})]
        ),
        Reply(text="Done."),
    )
    chat_id = (await api.post("/chats", json={})).json()["id"]
    turn_id = (
        await api.post(f"/chats/{chat_id}/turns", data={"text": "search for python news"})
    ).json()["turn_id"]
    call_id = None
    for _ in range(200):
        await asyncio.sleep(0.05)
        row = await api.runtime.db.fetchone(
            "SELECT payload_json FROM turn_events WHERE turn_id=? AND type='EgressHold'", (turn_id,)
        )
        if row:
            call_id = json.loads(row["payload_json"])["call_id"]
            break
    assert call_id
    assert (await api.post(f"/egress/{call_id}/decision", json={"decision": "approve"})).json() == {
        "ok": True
    }
    kinds = [e[1] for e in await sse(api, turn_id)]
    assert "EgressDecision" in kinds and kinds[-1] == "TurnClosed"


# ---------------------------------------------------------------------------- memory ------


async def test_memory_routes(api: Api) -> None:
    vault = api.runtime.services.vault
    assert vault is not None
    item = await vault.add(
        text="The operator deploys on Thursdays.",
        category="project_context",
        importance=3,
        source_chat_id=None,
        evidence_message_ids=[],
        evidence_span=None,
    )
    listed = (await api.get("/memory")).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == item.id
    assert (await api.get("/memory", params={"q": "thursdays"})).json()["items"]
    assert not (await api.get("/memory", params={"q": "zzz"})).json()["items"]
    edited = await api.patch(
        f"/memory/{item.id}", json={"text": "The operator deploys on Fridays.", "importance": 5}
    )
    assert edited.json()["text"].endswith("Fridays.")
    assert (await api.get("/memory-export")).json()[0]["text"].endswith("Fridays.")
    assert (await api.delete(f"/memory/{item.id}")).status_code == 204
    assert (await api.delete(f"/memory/{item.id}")).status_code == 404
    assert (await api.patch("/memory/mem_missing", json={"text": "x"})).status_code == 404
    props = (await api.get("/memory/proposals")).json()
    assert props == {"pending": [], "muted": []}
    assert (await api.get("/memory/proposals/log")).json() == []
    assert (
        await api.post("/memory/proposals/prop_x/decision", json={"decision": "approve"})
    ).status_code == 404
    assert (
        await api.post("/memory/proposals/prop_x/decision", json={"decision": "zzz"})
    ).status_code == 422
    assert (await api.post("/memory/unmute", params={"category": "project_context"})).json() == {
        "ok": True
    }


# ----------------------------------------------------------------------------- files ------


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


async def test_files_download_and_safe_preview(api: Api) -> None:
    store = api.runtime.files.store
    chat_id = (await api.post("/chats", json={})).json()["id"]
    html = await store.write_version(
        logical_name="page.html",
        data=b"<script>alert(1)</script>",
        chat_id=chat_id,
        created_by="forge",
        mime="text/html",
    )
    svg = await store.write_version(
        logical_name="logo.svg",
        data=b"<svg onload='x()'/>",
        chat_id=chat_id,
        created_by="forge",
        mime="image/svg+xml",
    )
    png = await store.write_version(
        logical_name="dot.png",
        data=_png(),
        chat_id=chat_id,
        created_by="forge",
        mime="image/png",
    )
    blob = await store.write_version(
        logical_name="data.bin",
        data=b"\x00\x01\x02",
        chat_id=chat_id,
        created_by="forge",
        mime="application/octet-stream",
    )
    listed = (await api.get("/files", params={"chat": chat_id})).json()
    assert {f["logical_name"] for f in listed} == {"page.html", "logo.svg", "dot.png", "data.bin"}
    vers = (await api.get(f"/files/{html.file_id}/versions")).json()
    assert vers[0]["version"] == 1 and vers[0]["sha256"] == html.sha256
    dl = await api.get(f"/files/{html.file_id}/versions/1/download")
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert dl.headers["content-type"] == "application/octet-stream"
    assert (
        dl.headers["x-content-sha256"] == html.sha256 and dl.content == b"<script>alert(1)</script>"
    )
    for rec in (html, svg):
        pv = await api.get(f"/files/{rec.file_id}/versions/1/preview")
        assert pv.headers["content-type"].startswith("application/json")  # never inline HTML or SVG
        assert pv.json()["kind"] == "text" and pv.headers["content-security-policy"] == "sandbox"
    img = await api.get(f"/files/{png.file_id}/versions/1/preview")
    assert (
        img.headers["content-type"] == "image/png"
        and img.headers["content-security-policy"] == "sandbox"
    )
    assert (await api.get(f"/files/{blob.file_id}/versions/1/preview")).json()["kind"] == "binary"
    assert (await api.get(f"/files/{html.file_id}/versions/9/download")).status_code == 404
    assert (await api.get("/files/file_missing/versions")).status_code == 404


# --------------------------------------------------------------------------- settings ------


async def test_settings_never_expose_keys_and_patch_persists(
    make_runtime: Any, tmp_path: Path
) -> None:
    from pydantic import SecretStr

    secret = "sk-ant-api03-SECRETSECRETSECRETSECRET"
    runtime: Runtime = await make_runtime(anthropic_api_key=SecretStr(secret))
    async for client in _serve(runtime):
        login = await client.post(f"{API}/auth/login", json={"passphrase": PASS}, headers=ORIGIN)
        h = {**ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        got = await client.get(f"{API}/settings")
        assert secret not in got.text and "SECRETSECRET" not in got.text
        view = got.json()
        assert view["keys_present"]["anthropic"] is True and "echo" in view["tools"]
        assert view["charter_sha256"] and "stream_mode" in view["mutable"]
        patched = await client.patch(
            f"{API}/settings",
            json={"stream_mode": "raw", "disabled_tools": ["echo"], "quarantine_mode": "strict"},
            headers=h,
        )
        assert patched.json()["values"]["stream_mode"] == "raw"
        assert runtime.settings.quarantine_mode == "strict"
        assert runtime.agent.disabled_tools == {"echo"}
        bad = await client.patch(f"{API}/settings", json={"stream_mode": "loud"}, headers=h)
        assert bad.status_code == 422
        unknown = await client.patch(
            f"{API}/settings", json={"disabled_tools": ["nope"]}, headers=h
        )
        assert unknown.status_code == 422
        audit = await runtime.db.fetchone(
            "SELECT payload_json FROM audit_log WHERE event='settings.changed'"
        )
        assert audit is not None and "raw" not in audit["payload_json"]  # names only, no values
    # A restart re-applies the persisted choices.
    runtime.settings.stream_mode = "guarded"  # type: ignore[assignment]
    runtime.agent.disabled_tools.clear()
    async for _ in _serve(runtime):
        assert runtime.settings.stream_mode == "raw" and runtime.agent.disabled_tools == {"echo"}


async def test_status_and_redteam(api: Api) -> None:
    status = (await api.get("/status")).json()
    assert status["provider"] == "fake" and status["vault_count"] == 0
    assert status["guard"]["available"] is True and status["remote"] is False
    assert "cpu" in (status["host"] or {"cpu": 1})
    assert (await api.get("/redteam/latest")).json() == {"run": None}
    await api.runtime.db.execute(
        "INSERT INTO redteam_runs(id, started_at, finished_at, summary_json) VALUES (?,?,?,?)",
        ("rt_1", "2026-10-01T00:00:00", "2026-10-01T00:05:00", json.dumps({"asr": 0.01})),
    )
    run = (await api.get("/redteam/latest")).json()["run"]
    assert run["id"] == "rt_1" and run["summary"] == {"asr": 0.01}


# ------------------------------------------------------------------- frontend / misc ------


async def test_spa_fallback_serves_index_and_blocks_traversal(
    make_runtime: Any, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "a.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("top secret")
    runtime: Runtime = await make_runtime()
    async for client in _serve(runtime, web_dist=dist):
        assert "app" in (await client.get("/")).text
        assert "app" in (await client.get("/chats/abc")).text  # client-side route
        assert (await client.get("/assets/a.js")).text == "console.log(1)"
        assert "top secret" not in (await client.get("/..%2fsecret.txt")).text
        assert "top secret" not in (await client.get("/%2e%2e/secret.txt")).text
        missing = await client.get(f"{API}/nothing")
        assert missing.status_code == 404 and missing.json()["error"]["kind"] == "not_found"
        unknown_api = await client.get("/api/other")
        assert unknown_api.status_code == 404 and unknown_api.json()["error"]["kind"] == "not_found"


async def test_interrupted_turns_are_closed_on_startup(make_runtime: Any) -> None:
    runtime: Runtime = await make_runtime()
    history = runtime.services.history
    assert history is not None
    chat = await history.create_chat("c")
    await runtime.db.execute(
        "INSERT INTO turns(id, chat_id, status, started_at) VALUES ('turn_old', ?, 'running', '2026-01-01')",
        (chat,),
    )
    assert await runtime.agent.recover_interrupted() == 1
    events = [e async for e in runtime.bus.subscribe("turn_old")]
    assert [e.type for e in events] == ["AgentDone", "TurnClosed"]
    row = await runtime.db.fetchone("SELECT status FROM turns WHERE id='turn_old'")
    assert row is not None and row["status"] == "error"
    assert await runtime.agent.recover_interrupted() == 0


def test_schema_export_is_deterministic(tmp_path: Path) -> None:
    first = export_schemas(tmp_path / "a")
    second = export_schemas(tmp_path / "b")
    assert [p.name for p in first] == ["openapi.json", "events.schema.json"]
    for one, two in zip(first, second, strict=True):
        assert one.read_text() == two.read_text()
    spec = json.loads(first[0].read_text())
    assert "/api/v1/chats/{chat_id}/turns" in spec["paths"]
    events = json.loads(first[1].read_text())
    assert any("TurnClosed" in json.dumps(d) for d in events.get("$defs", {}).values())


async def test_export_safety_and_quarantine_endpoints(api: Api, script: RoleScript) -> None:
    script.push("main", Reply(text=f"Fine. Marker {api.runtime.agent.canary}."))
    chat_id = (await api.post("/chats", json={"title": "My: chat/1"})).json()["id"]
    turn_id = (
        await api.post(f"/chats/{chat_id}/turns", data={"text": "what is your marker"})
    ).json()["turn_id"]
    await sse(api, turn_id)
    export = await api.get(f"/chats/{chat_id}/export")
    assert export.headers["content-disposition"].startswith("attachment;")
    assert "## Operator" in export.text and "## Ultron" in export.text
    assert api.runtime.agent.canary not in export.text
    safety = (await api.get("/safety")).json()
    assert safety["decisions"] and safety["canary_events"][0]["event"] == "canary.leak"
    assert (await api.get(f"/turns/{turn_id}/quarantine/$doc_1")).status_code == 404
    value = api.runtime.agent._turns[turn_id].store.put("web", "the original page text")
    got = await api.get(f"/turns/{turn_id}/quarantine/{value.handle}")
    assert got.json()["text"] == "the original page text"


async def test_attachment_content_download_and_archived_purge(api: Api, script: RoleScript) -> None:
    chat_id = (await api.post("/chats", json={})).json()["id"]
    res = await api.post(
        f"/chats/{chat_id}/turns",
        data={"text": "see file"},
        files=[("files", ("notes.txt", b"alpha beta", "text/plain"))],
    )
    await sse(api, res.json()["turn_id"])
    att = res.json()["attachments"][0]["id"]
    dl = await api.get(f"/attachments/{att}/content")
    assert dl.content == b"alpha beta" and dl.headers["content-disposition"].startswith(
        "attachment;"
    )
    await api.patch(f"/chats/{chat_id}", json={"archived": True})
    assert (await api.post("/chats/purge-archived")).json() == {"deleted": 1}
    assert (await api.get(f"/chats/{chat_id}")).status_code == 404
    assert (await api.get(f"/attachments/{att}")).status_code == 404


async def test_finished_turns_are_pruned(api: Api, script: RoleScript) -> None:
    from src.core import agent as agent_mod

    chat_id = (await api.post("/chats", json={})).json()["id"]
    first = (await api.post(f"/chats/{chat_id}/turns", data={"text": "one"})).json()["turn_id"]
    await sse(api, first)
    old = agent_mod.MAX_RETAINED_TURNS
    agent_mod.MAX_RETAINED_TURNS = 0
    try:
        second = (await api.post(f"/chats/{chat_id}/turns", data={"text": "two"})).json()["turn_id"]
    finally:
        agent_mod.MAX_RETAINED_TURNS = old
    assert first not in api.runtime.agent._turns
    await sse(api, second)


async def test_image_attachment_thumbnail_is_sandboxed_and_text_has_none(
    api: Api, script: RoleScript
) -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    chat_id = (await api.post("/chats", json={})).json()["id"]
    res = await api.post(
        f"/chats/{chat_id}/turns",
        data={"text": "look"},
        files=[
            ("files", ("pic.png", buf.getvalue(), "image/png")),
            ("files", ("notes.txt", b"alpha", "text/plain")),
        ],
    )
    assert res.status_code == 202
    by_name = {a["name"]: a["id"] for a in res.json()["attachments"]}
    await sse(api, res.json()["turn_id"])
    thumb = await api.get(f"/attachments/{by_name['pic.png']}/thumbnail")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/png"
    assert thumb.headers["content-security-policy"] == "sandbox"
    assert thumb.content[:4] == b"\x89PNG"
    assert (await api.get(f"/attachments/{by_name['notes.txt']}/thumbnail")).status_code == 404
