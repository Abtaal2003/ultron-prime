"""A scripted provider for offline tests, the demo server, and end-to-end runs.

Responses come from a queue of ``Reply`` items or a callable. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from src.core.errors import ProviderError, StructuredOutputError
from src.core.llm.base import (
    ChatRequest,
    ChatResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    T,
    TextPart,
    ToolUsePart,
    Usage,
)
from src.core.llm.structured import validate_structured


@dataclass(slots=True)
class Reply:
    """One scripted reply: text and optional tool calls, or an error to raise."""

    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None
    structured: dict[str, Any] | BaseModel | str | None = None
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))


Script = list[Reply] | Callable[[ChatRequest], Reply]


class FakeProvider:
    """Deterministic provider. Records every request it receives."""

    def __init__(
        self,
        script: Script | None = None,
        *,
        name: str = "fake",
        caps: ProviderCapabilities | None = None,
        default_text: str = "ok",
    ) -> None:
        self.name = name
        self._script = script if script is not None else []
        self._caps = caps or ProviderCapabilities(
            structured_output="native_strict",
            structured_output_with_tools=True,
            vision=True,
            pdf_input=True,
            supports_effort=True,
            thinking_portable_to=frozenset({name}),
        )
        self._default_text = default_text
        self.requests: list[ChatRequest] = []
        self._counter = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Return the configured capabilities."""
        return self._caps

    def _next(self, request: ChatRequest) -> Reply:
        self.requests.append(request)
        if callable(self._script):
            reply = self._script(request)
        elif self._script:
            reply = self._script.pop(0)
        else:
            reply = Reply(text=self._default_text)
        if reply.error is not None:
            raise reply.error
        return reply

    def _response(self, reply: Reply, request: ChatRequest) -> ChatResponse:
        parts: list[Any] = []
        if reply.text:
            parts.append(TextPart(text=reply.text))
        for name, args in reply.tool_calls:
            self._counter += 1
            parts.append(ToolUsePart(id=f"call_fake_{self._counter}", name=name, arguments=args))
        return ChatResponse(
            provider=self.name,
            model=request.model,
            message=Message(role="assistant", parts=parts),
            stop_reason="tool_use" if reply.tool_calls else "end_turn",
            usage=reply.usage,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Return the next scripted reply."""
        return self._response(self._next(request), request)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream the next scripted reply word by word."""
        reply = self._next(request)
        words = reply.text.split(" ")
        for i, word in enumerate(words):
            if word:
                yield StreamChunk(kind="text", delta=word + (" " if i < len(words) - 1 else ""))
        response = self._response(reply, request)
        for call in response.tool_calls:
            yield StreamChunk(kind="tool_call", tool_call=call)
        yield StreamChunk(kind="done", response=response)

    async def structured(self, request: ChatRequest, schema: type[T]) -> tuple[T, Usage]:
        """Validate the scripted structured payload against ``schema``."""
        reply = self._next(request)
        payload = reply.structured if reply.structured is not None else reply.text
        if isinstance(payload, BaseModel):
            payload = payload.model_dump()
        if payload in ("", None):
            raise StructuredOutputError("fake provider has no structured payload")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return validate_structured(schema, text), reply.usage


def failing(error: ProviderError, times: int = 1_000) -> list[Reply]:
    """A script that raises ``error`` ``times`` times."""
    return [Reply(error=error) for _ in range(times)]
