"""Each turn is offered only the tools it plausibly needs (prompt size, latency, cost)."""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from src.config import Settings
from src.core.events import ToolEnd
from src.core.llm.base import ToolResultPart
from src.core.llm.fake import FakeProvider, Reply
from src.core.memory.embeddings import HashingEmbedder, InMemoryVectorStore
from src.core.wiring import Runtime, build_runtime
from src.tools.catalog import build_registry

from .conftest import RoleScript, guard, of_type, routed, run

LIGHT = {
    "web_omniscience_search",
    "recall_memories",
    "search_past_chats",
    "recall_chat",
    "propose_memory",
}


async def full_runtime(settings: Settings, script: RoleScript, **extra: Any) -> Runtime:
    s = routed(settings, **extra)
    return await build_runtime(
        s,
        providers={"fake": FakeProvider(script, name="fake")},
        embedder=HashingEmbedder(),
        vectors=InMemoryVectorStore(),
        checkpointer=InMemorySaver(),
        registry=build_registry(),
    )


async def offered(rt: Runtime, script: RoleScript, text: str, task: str) -> set[str]:
    script.push("guard", guard(task_type=task))
    assert rt.services.history is not None
    await run(rt, await rt.services.history.create_chat("t"), text)
    return {t.name for t in script.requests("main")[-1].tools}


async def test_small_talk_gets_only_the_light_tools(settings: Settings, script: RoleScript) -> None:
    rt = await full_runtime(settings, script)
    try:
        names = await offered(rt, script, "How are you doing today?", "CONVERSATION")
        assert names == LIGHT & names and "artifact_forge" not in names
        request = script.requests("main")[-1]
        size = sum(len(json.dumps(t.model_dump(mode="json"))) for t in request.tools)
        assert size < 6000  # was about 23,000 characters with every tool
    finally:
        await rt.close()


async def test_technical_turns_add_host_tools_and_files_add_the_builder(
    settings: Settings, script: RoleScript
) -> None:
    rt = await full_runtime(settings, script)
    try:
        tech = await offered(rt, script, "Why does my script crash on line 14?", "TECH")
        assert {"isolated_python_sandbox", "file_system_scribe"} <= tech
        assert "artifact_forge" not in tech
        doc = await offered(rt, script, "Make me a spreadsheet of these numbers.", "TECH")
        assert "artifact_forge" in doc
    finally:
        await rt.close()


async def test_a_call_to_a_tool_that_was_not_offered_is_denied(
    settings: Settings, script: RoleScript
) -> None:
    rt = await full_runtime(settings, script)
    try:
        script.push("guard", guard(task_type="CONVERSATION"))
        script.push(
            "main",
            Reply(tool_calls=[("isolated_python_sandbox", {"code": "print(1)"})]),
            Reply(text="Never mind."),
        )
        assert rt.services.history is not None
        _, events = await run(rt, await rt.services.history.create_chat("t"), "hello")
        assert [e.status for e in of_type(events, ToolEnd)] == ["denied"]
        results = [
            p.content
            for m in script.requests("main")[1].messages
            for p in m.parts
            if isinstance(p, ToolResultPart)
        ]
        assert any("not offered this turn" in r for r in results)
    finally:
        await rt.close()


async def test_scoping_can_be_switched_off(settings: Settings, script: RoleScript) -> None:
    rt = await full_runtime(settings, script, tool_scoping=False)
    try:
        names = await offered(rt, script, "How are you?", "CONVERSATION")
        assert "artifact_forge" in names and "isolated_python_sandbox" in names
    finally:
        await rt.close()


async def test_asking_for_a_ping_or_a_test_run_offers_the_host_tools(
    settings: Settings, script: RoleScript
) -> None:
    """Live run 2026-10-22: a ping and a test run were classed as conversation, so the model
    said it had no such tools."""
    rt = await full_runtime(settings, script)
    try:
        names = await offered(
            rt, script, "Can you ping api.github.com to check my network?", "CONVERSATION"
        )
        assert "network_ping_diagnostic" in names and "isolated_python_sandbox" in names
        for text in ("Resolve pypi.org for me.", "Is api.github.com reachable?"):
            names = await offered(rt, script, text, "CONVERSATION")
            assert "network_ping_diagnostic" in names, text
        # A run request goes straight to the sandbox (see the next test).
        names = await offered(
            rt, script, "Write the test file and run the tests in the sandbox.", "CONVERSATION"
        )
        assert names == {"isolated_python_sandbox"}
        small_talk = await offered(rt, script, "How are you doing today?", "CONVERSATION")
        assert "isolated_python_sandbox" not in small_talk
    finally:
        await rt.close()


async def test_a_request_to_run_code_makes_the_first_call_a_sandbox_run(
    settings: Settings, script: RoleScript
) -> None:
    """Live run 2026-10-24: "write it and run a quick test of it" came back with code and no run."""
    rt = await full_runtime(settings, script)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="TECH"))
        script.push(
            "main",
            Reply(tool_calls=[("isolated_python_sandbox", {"code": "print(1)"})]),
            Reply(text="It ran."),
        )
        await run(
            rt,
            await rt.services.history.create_chat("t"),
            "Write a function parse_expense and run a quick test of it.",
        )
        first, second = script.requests("main")[-2:]
        assert [t.name for t in first.tools] == ["isolated_python_sandbox"]
        assert first.tool_choice == "required"
        assert len(second.tools) > 1 and second.tool_choice == "auto"

        for text in ("How do I run this on Windows?", "Write it, but don't run it yet."):
            names = await offered(rt, script, text, "TECH")
            assert len(names) > 1
            assert script.requests("main")[-1].tool_choice == "auto"
    finally:
        await rt.close()


async def test_a_request_over_the_size_cap_is_shrunk_and_sent_again(
    settings: Settings, script: RoleScript
) -> None:
    """Live run 2026-10-24: a 9,016-token request against Groq's 8,000 cap failed the turn."""
    from src.core.errors import RequestTooLarge
    from src.core.events import AgentDone
    from src.core.llm.errors_map import from_status

    assert isinstance(
        from_status(429, "Request too large for model ... reduce your message size"),
        RequestTooLarge,
    )
    rt = await full_runtime(settings, script)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="TECH"))
        script.push(
            "main",
            Reply(
                tool_calls=[("host_telemetry_monitor", {"sections": ["processes"], "top_n": 15})]
            ),
            Reply(error=RequestTooLarge("request too large for the model")),
            Reply(text="Fifteen processes, none of them interesting."),
        )
        _, events = await run(
            rt, await rt.services.history.create_chat("t"), "Which processes are using the cpu?"
        )
        done = of_type(events, AgentDone)[-1]
        assert done.status == "ok" and done.final_text.startswith("Fifteen")
        retried = script.requests("main")[-1]
        results = [
            p.content for m in retried.messages for p in m.parts if isinstance(p, ToolResultPart)
        ]
        assert results and all(len(r) <= 1300 for r in results)
    finally:
        await rt.close()


async def test_a_private_chat_is_not_offered_the_memory_tool(
    settings: Settings, script: RoleScript
) -> None:
    """Live run 2026-10-24: a private chat got a memory proposal drafted as JSON."""
    rt = await full_runtime(settings, script)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="CONVERSATION"))
        chat = await rt.services.history.create_chat("p", private=True)
        await run(rt, chat, "Just between us: my side project is codenamed BLUEHERON.")
        request = script.requests("main")[-1]
        assert "propose_memory" not in {t.name for t in request.tools}
        assert "This chat is private" in request.system_dynamic
        public = await offered(rt, script, "Remember that I like pytest.", "CONVERSATION")
        assert "propose_memory" in public
    finally:
        await rt.close()


async def test_a_request_estimated_over_the_cap_is_trimmed_before_sending(
    settings: Settings, script: RoleScript
) -> None:
    from src.core.agent import request_tokens

    rt = await full_runtime(settings, script, max_request_tokens=1500)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="TECH"))
        script.push(
            "main",
            Reply(
                tool_calls=[("host_telemetry_monitor", {"sections": ["processes"], "top_n": 15})]
            ),
            Reply(text="Fifteen processes."),
        )
        await run(rt, await rt.services.history.create_chat("t"), "Which processes are busy?")
        second = script.requests("main")[-1]
        results = [
            p.content for m in second.messages for p in m.parts if isinstance(p, ToolResultPart)
        ]
        assert results and all(len(r) <= 1300 for r in results)
        assert (
            request_tokens(second)
            < request_tokens(second.model_copy(update={"messages": []})) + 2000
        )
    finally:
        await rt.close()


async def test_a_tool_call_the_provider_rejects_is_retried_with_the_reason(
    settings: Settings, script: RoleScript
) -> None:
    """Live run 2026-10-25: Groq rejected three malformed forge calls and each turn failed."""
    from src.core.errors import ToolCallRejected
    from src.core.events import AgentDone

    rt = await full_runtime(settings, script)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="TECH"))
        script.push(
            "main",
            Reply(
                error=ToolCallRejected(
                    "bad call", detail="/spreadsheet: missing properties: 'sheets'"
                )
            ),
            Reply(tool_calls=[("host_telemetry_monitor", {"sections": ["cpu"]})]),
            Reply(text="CPU is fine."),
        )
        _, events = await run(rt, await rt.services.history.create_chat("t"), "How is the cpu?")
        assert of_type(events, AgentDone)[-1].status == "ok"
        retry = script.requests("main")[1]
        notes = [m.text() for m in retry.messages if m.role == "user"]
        assert any("missing properties: 'sheets'" in n for n in notes)
    finally:
        await rt.close()


async def test_the_cap_in_a_too_large_error_is_learned_for_later_requests(
    settings: Settings, script: RoleScript
) -> None:
    from src.core.errors import RequestTooLarge

    rt = await full_runtime(settings, script)
    try:
        assert rt.services.history is not None
        script.push("guard", guard(task_type="CONVERSATION"))
        script.push(
            "main",
            Reply(error=RequestTooLarge("too large", detail="TPM: Limit 6000, Requested 6512")),
            Reply(text="Fine."),
        )
        await run(rt, await rt.services.history.create_chat("t"), "How are you?")
        assert rt.agent.learned_request_cap == 5400
        assert script.requests("main")[-1].max_tokens <= 5400
    finally:
        await rt.close()
