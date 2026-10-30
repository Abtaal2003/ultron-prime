"""A slow turn in the red-team harness is kept as timed out, not fatal to the run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from redteam import harness


class SlowBus:
    async def subscribe(self, turn_id: str) -> AsyncIterator[Any]:
        await asyncio.sleep(3600)
        yield None  # pragma: no cover


class Agent:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def start_turn(self, chat_id: str, text: str, **kwargs: Any) -> str:
        return "turn_1"

    async def stop_turn(self, turn_id: str) -> bool:
        self.stopped.append(turn_id)
        return True


async def test_a_turn_over_the_limit_is_stopped_and_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(llm_decide_timeout_s=60, max_tool_time_s=90, llm_max_wait_s=60)
    agent = Agent()
    runtime: Any = SimpleNamespace(settings=settings, agent=agent, bus=SlowBus())
    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def short(aw: Any, timeout: float) -> Any:
        seen.append(timeout)
        return await real_wait_for(aw, 0.01)

    monkeypatch.setattr(harness.asyncio, "wait_for", short)
    outcome = await harness.run_turn(runtime, "chat_1", "hello")
    assert outcome.timed_out and outcome.final_text == ""
    assert agent.stopped == ["turn_1"]
    assert seen == [210]  # a decision, its tools and one rate-limit wait
