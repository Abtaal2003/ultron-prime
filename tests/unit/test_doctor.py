"""``ultron doctor``: plain findings, never a key, live pings explained."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from src import doctor
from src.config import Settings


def make(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "sqlite_path": str(tmp_path / "data" / "u.db"),
        "workspace_root": str(tmp_path / "ws"),
        "embedding_backend": "hashing",
    }
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[call-arg, arg-type]


def by_topic(findings: list[doctor.Finding]) -> dict[str, doctor.Finding]:
    return {f.topic: f for f in findings}


def test_routes_fall_back_to_a_provider_with_a_key(tmp_path: Path) -> None:
    s = make(tmp_path, groq_api_key="gsk_secret_value")
    found = by_topic(doctor.check(s))
    assert found["route main"].message.startswith("groq:openai/gpt-oss-120b")
    assert "falling back" in found["route main"].message
    assert found["route guard"].message == "groq:openai/gpt-oss-20b"
    assert found["free tier"].level == "warn"
    assert all("gsk_secret_value" not in f.message for f in found.values())


def test_free_tier_hint_goes_once_the_cap_is_set(tmp_path: Path) -> None:
    s = make(tmp_path, groq_api_key="k", max_request_tokens=8000)
    assert "free tier" not in by_topic(doctor.check(s))


def test_unreachable_roles_are_failures(tmp_path: Path) -> None:
    s = make(
        tmp_path,
        gemini_api_key="k",
        route_guard="groq:openai/gpt-oss-20b",
        fallback_chain=["groq:openai/gpt-oss-120b"],
    )
    found = by_topic(doctor.check(s))
    assert found["route guard"].level == "fail"


def test_folders_web_and_embeddings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked = tmp_path / "locked"
    locked.write_text("a file, not a folder")
    s = make(tmp_path, anthropic_api_key="k", workspace_root=str(locked / "ws"))
    found = by_topic(doctor.check(s))
    assert found["workspace"].level == "fail" and found["data folder"].level == "ok"
    assert found["embeddings"].level == "warn"
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    found = by_topic(doctor.check(make(tmp_path, anthropic_api_key="k", embedding_backend="local")))
    assert (
        found["embeddings"].level == "fail" and "--extra embeddings" in found["embeddings"].message
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    found = by_topic(doctor.check(make(tmp_path, anthropic_api_key="k", embedding_backend="local")))
    assert found["embeddings"].level == "ok"
    assert "web app" not in by_topic(doctor.check(make(tmp_path, container=True)))


def test_sandbox_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import sandbox

    assert doctor._sandbox_finding(make(tmp_path, sandbox_network="host")).level == "warn"
    assert (
        doctor._sandbox_finding(make(tmp_path, sandbox_network="host", container=True)).level
        == "ok"
    )
    monkeypatch.setattr(sandbox, "network_isolation_prefix", lambda: None)
    assert doctor._sandbox_finding(make(tmp_path)).level == "warn"
    monkeypatch.setattr(sandbox, "network_isolation_prefix", lambda: ["unshare"])
    assert doctor._sandbox_finding(make(tmp_path)).message == "code runs with no network access"


def test_errors_are_explained() -> None:
    assert "daily quota" in doctor._explain("Rate limit reached ... tokens per day (TPD)")
    assert "key was refused" in doctor._explain("401 Invalid API Key")
    assert "model name" in doctor._explain("404 model does not exist")
    assert doctor._explain("boom").startswith("failed: boom")


def test_report_exit_code() -> None:
    lines: list[str] = []
    code = doctor.report([doctor.Finding("warn", "a", "b")], lines.append)
    assert code == 0 and lines[0] == "[warn] a: b\n"
    assert doctor.report([doctor.Finding("fail", "a", "b")], lines.append) == 1


async def test_live_pings_each_distinct_route_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core import wiring

    calls: list[str] = []

    class Fake:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def chat(self, request: Any) -> None:
            calls.append(request.model)
            if self.fail:
                raise RuntimeError("Invalid API Key")

    monkeypatch.setattr(
        wiring, "build_providers", lambda s: {"groq": Fake(False), "gemini": Fake(True)}
    )
    s = make(tmp_path, groq_api_key="k", gemini_api_key="k")
    found = await doctor.live(s)
    assert len(calls) == len(set(calls))
    levels = {f.topic: f.level for f in found}
    assert levels["live groq:openai/gpt-oss-20b"] == "ok"
    assert levels["live gemini:gemini-3.5-flash"] == "fail"


def test_the_free_tier_preset_is_a_valid_setup(tmp_path: Path) -> None:
    from src.config import PROJECT_ROOT

    preset = PROJECT_ROOT / ".env.free.example"
    env = tmp_path / ".env"
    env.write_text(
        preset.read_text().replace("GROQ_API_KEY=\n", "GROQ_API_KEY=k\n")
        + f"\nSQLITE_PATH={tmp_path}/d/u.db\nWORKSPACE_ROOT={tmp_path}/ws\nEMBEDDING_BACKEND=hashing\n"
    )
    s = Settings(_env_file=str(env))  # type: ignore[call-arg]
    found = by_topic(doctor.check(s))
    assert s.max_request_tokens == 8000
    assert found["route main"].message == "groq:openai/gpt-oss-120b"
    assert "free tier" not in found
    assert not [f for f in found.values() if f.level == "fail"]
