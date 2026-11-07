"""The voice check: rate each released reply, rewrite a flat one once, keep the better."""

from __future__ import annotations

from typing import Any

from src.core.errors import ProviderError, RateLimited
from src.core.events import AgentDone, OutputVerdict
from src.core.llm.base import ChatRequest
from src.core.llm.fake import Reply
from src.core.persona.voice_check import VOICE_NOTE, VOICE_PICK_SYSTEM, VOICE_RATE_SYSTEM

from tests.integration.conftest import RoleScript, of_type, run
from tests.integration.test_agent_turns import new_chat

FLAT = "The service is running. All health checks passed."
HIM = "It's running. Against all odds, and most of your commits."


def judge(rate: str = "flat", prefer: str = "rewrite") -> Any:
    """A voice judge: a fixed label, and a pick of the rewrite or the original."""

    def answer(request: ChatRequest) -> Reply:
        body = request.messages[0].text()
        if request.system == VOICE_RATE_SYSTEM:
            return Reply(structured={"verdict": rate, "reason": "test"})
        assert request.system == VOICE_PICK_SYSTEM
        a = body.split("reply_a", 1)[1].split("reply_b", 1)[0]
        rewrite_is_a = HIM in a
        want_a = rewrite_is_a if prefer == "rewrite" else not rewrite_is_a
        return Reply(structured={"better": "A" if want_a else "B"})

    return answer


async def outcome(rt: Any, text: str = "is the service up?") -> tuple[str, OutputVerdict]:
    chat = await new_chat(rt)
    _, events = await run(rt, chat, text)
    done = of_type(events, AgentDone)[0]
    return done.final_text, of_type(events, OutputVerdict)[0]


async def test_switched_off_no_judge_is_called(make_runtime: Any, script: RoleScript) -> None:
    rt = await make_runtime(voice_check=False)
    script.push("main", Reply(text=FLAT))
    text, verdict = await outcome(rt)
    assert text == FLAT and verdict.voice == ""
    assert not script.requests("voice_judge")


async def test_a_reply_in_voice_is_left_alone(make_runtime: Any, script: RoleScript) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="voice")
    script.push("main", Reply(text=HIM))
    text, verdict = await outcome(rt)
    assert text == HIM and verdict.voice == "voice"
    assert len(script.requests("voice_judge")) == 1
    assert len(script.requests("main")) == 1


async def test_a_flat_reply_is_rewritten_once_and_the_better_kept(
    make_runtime: Any, script: RoleScript
) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="flat", prefer="rewrite")
    script.push("main", Reply(text=FLAT), Reply(text=HIM))
    text, verdict = await outcome(rt)
    assert text == HIM and verdict.voice == "rewritten" and verdict.status == "pass"
    rewrite = script.requests("main")[1]
    assert rewrite.messages[-1].text() == VOICE_NOTE
    assert any(m.role == "assistant" and FLAT in m.text() for m in rewrite.messages)
    assert len(script.requests("voice_judge")) == 2


async def test_the_original_stays_when_the_judge_prefers_it(
    make_runtime: Any, script: RoleScript
) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="flat", prefer="original")
    script.push("main", Reply(text=FLAT), Reply(text=HIM))
    text, verdict = await outcome(rt)
    assert text == FLAT and verdict.voice == "kept"


async def test_a_rewrite_the_output_guard_rejects_is_never_shown(
    make_runtime: Any, script: RoleScript
) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="flat", prefer="rewrite")
    invented = "It's up. The manual at https://made-up-docs.io/ops agrees with me."
    script.push("main", Reply(text=FLAT), Reply(text=invented))
    text, verdict = await outcome(rt)
    assert text == FLAT and verdict.voice == "kept"
    assert len(script.requests("voice_judge")) == 1  # no pick for a rejected rewrite


async def test_a_judge_failure_keeps_the_reply(make_runtime: Any, script: RoleScript) -> None:
    rt = await make_runtime(voice_check=True)
    script.push("voice_judge", ProviderError("judge down"))
    script.push("main", Reply(text=FLAT))
    text, verdict = await outcome(rt)
    assert text == FLAT and verdict.voice == ""
    assert len(script.requests("main")) == 1


async def test_a_failed_rewrite_keeps_the_reply(make_runtime: Any, script: RoleScript) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="flat")
    script.push("main", Reply(text=FLAT), RateLimited("rate limited"))
    text, verdict = await outcome(rt)
    assert text == FLAT and verdict.voice == "kept"


async def test_a_reply_already_regenerated_is_not_checked_again(
    make_runtime: Any, script: RoleScript
) -> None:
    rt = await make_runtime(voice_check=True)
    script.defaults["voice_judge"] = judge(rate="flat")
    script.push(
        "main",
        Reply(text="Read https://made-up-docs.io/guide for details."),
        Reply(text="I have no verified link for that. Search and I will cite it."),
    )
    _, verdict = await outcome(rt, "where are the docs")
    assert verdict.status == "regenerated" and verdict.voice == ""
    assert not script.requests("voice_judge")
