"""The voice judge's pick maps back correctly whichever order the replies were shown in."""

from __future__ import annotations

from typing import Any

from src.config import Settings
from src.core.llm.base import ChatRequest
from src.core.persona.voice_check import VoiceCheck, VoicePick


class PicksText:
    """A stub router whose judge always picks the reply containing ``wanted``."""

    def __init__(self, wanted: str) -> None:
        self.wanted = wanted
        self.orders: list[bool] = []

    async def structured(self, role: str, request: ChatRequest, schema: Any, ctx: Any) -> Any:
        body = request.messages[0].text()
        first = body.split("reply_a", 1)[1].split("reply_b", 1)[0]
        self.orders.append(self.wanted in first)
        return VoicePick(better="A" if self.wanted in first else "B")


async def test_pick_maps_back_in_both_orders() -> None:
    settings = Settings(_env_file=None)
    for wanted in ("rewrite", "original"):
        router = PicksText(f"<{wanted}>")
        check = VoiceCheck(router, settings)  # type: ignore[arg-type]
        for i in range(12):
            original, rewrite = f"<original> {i}", f"<rewrite> {i}"
            assert await check.prefer_rewrite("q", original, rewrite, None) is (wanted == "rewrite")
        assert set(router.orders) == {True, False}  # both orders were shown
