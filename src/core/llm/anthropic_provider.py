"""Anthropic Claude adapter (spec 7.1).

5.x models take an ``effort`` control and adaptive thinking, and reject ``temperature``,
``top_p``, and ``top_k``. Structured output uses ``output_config.format``. Thinking blocks carry
signatures and are only valid for Claude, so they are tagged with family ``"claude"``.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

from src.core.errors import StructuredOutputError
from src.core.llm.base import (
    ChatRequest,
    ChatResponse,
    DocumentPart,
    ImagePart,
    Message,
    ProviderCapabilities,
    StopReason,
    StreamChunk,
    T,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    Usage,
)
from src.core.llm.errors_map import from_status
from src.core.llm.structured import to_strict_schema, validate_structured

FAMILY = "claude"
_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


class AnthropicProvider:
    """Adapter over ``anthropic.AsyncAnthropic``."""

    name = "anthropic"

    def __init__(self, api_key: str, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key, max_retries=0)

    # ------------------------------------------------------------------------ capabilities --
    def capabilities(self, model: str) -> ProviderCapabilities:
        """Claude 5.x: effort, adaptive thinking, vision, PDFs, caching, native structured."""
        legacy = any(tag in model for tag in ("claude-3", "haiku-4", "sonnet-4", "opus-4"))
        return ProviderCapabilities(
            tool_calling=True,
            forced_tool_choice="opus-5" not in model and "sonnet-5" not in model,
            parallel_tool_calls=True,
            streaming=True,
            structured_output="native_strict",
            structured_output_with_tools=True,
            supports_temperature=legacy,
            supports_effort=not legacy,
            thinking_portable_to=frozenset({FAMILY}),
            prompt_caching=True,
            vision=True,
            pdf_input=True,
            max_image_bytes=5 * 1024 * 1024,
            max_context=1_000_000 if not legacy else 200_000,
        )

    # --------------------------------------------------------------------------- encoding --
    @staticmethod
    def _encode_part(part: Any) -> dict[str, Any] | None:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text} if part.text else None
        if isinstance(part, ImagePart):
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": base64.b64encode(part.data).decode(),
                },
            }
        if isinstance(part, DocumentPart):
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": base64.b64encode(part.data).decode(),
                },
            }
        if isinstance(part, ThinkingPart):
            if part.family != FAMILY:
                return None
            if part.redacted:
                return {"type": "redacted_thinking", "data": part.signature or ""}
            return {"type": "thinking", "thinking": part.text, "signature": part.signature or ""}
        if isinstance(part, ToolUsePart):
            return {"type": "tool_use", "id": part.id, "name": part.name, "input": part.arguments}
        if isinstance(part, ToolResultPart):
            return {
                "type": "tool_result",
                "tool_use_id": part.tool_use_id,
                "content": part.content,
                "is_error": part.is_error,
            }
        return None

    def _encode_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            content = [c for p in m.parts if (c := self._encode_part(p)) is not None]
            if content:
                out.append({"role": m.role, "content": content})
        return out

    def build_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        """Translate ``request`` into ``messages.create`` keyword arguments."""
        caps = self.capabilities(request.model)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": self._encode_messages(request.messages),
            "timeout": request.timeout_s,
        }
        if request.system:
            block: dict[str, Any] = {"type": "text", "text": request.system}
            if request.cache_system and caps.prompt_caching:
                block["cache_control"] = {"type": "ephemeral"}
            blocks = [block]
            if request.system_dynamic:
                blocks.append({"type": "text", "text": request.system_dynamic})
            kwargs["system"] = blocks
        elif request.system_dynamic:
            kwargs["system"] = [{"type": "text", "text": request.system_dynamic}]
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            if request.tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif request.tool_choice == "required" and caps.forced_tool_choice:
                kwargs["tool_choice"] = {"type": "any"}
        output_config: dict[str, Any] = {}
        if caps.supports_effort and request.sampling.effort:
            output_config["effort"] = request.sampling.effort
        if output_config:
            kwargs["output_config"] = output_config
        if (
            caps.supports_effort
            and request.allow_thinking
            and request.sampling.effort not in {None, "low"}
        ):
            kwargs["thinking"] = {"type": "adaptive"}
        if caps.supports_temperature and request.sampling.temperature is not None:
            kwargs["temperature"] = request.sampling.temperature
        return kwargs

    # --------------------------------------------------------------------------- decoding --
    @staticmethod
    def _decode(message: Any, provider: str) -> ChatResponse:
        parts: list[Any] = []
        for block in message.content:
            kind = getattr(block, "type", "")
            if kind == "text":
                parts.append(TextPart(text=block.text))
            elif kind == "thinking":
                parts.append(
                    ThinkingPart(text=block.thinking, signature=block.signature, family=FAMILY)
                )
            elif kind == "redacted_thinking":
                parts.append(ThinkingPart(signature=block.data, family=FAMILY, redacted=True))
            elif kind == "tool_use":
                parts.append(
                    ToolUsePart(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )
        usage = getattr(message, "usage", None)
        return ChatResponse(
            provider=provider,
            model=getattr(message, "model", ""),
            message=Message(role="assistant", parts=parts),
            stop_reason=_STOP.get(str(getattr(message, "stop_reason", "")), "other"),
            usage=Usage(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
        )

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        import anthropic

        if isinstance(exc, anthropic.APIStatusError):
            headers = getattr(getattr(exc, "response", None), "headers", None)
            return from_status(exc.status_code, str(exc), headers)
        if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
            return from_status(None, type(exc).__name__)
        return exc

    # ------------------------------------------------------------------------------ calls --
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Complete a request."""
        try:
            message = await self._client.messages.create(**self.build_kwargs(request))
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return self._decode(message, self.name)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream text and thinking deltas; the last chunk carries the whole response."""
        try:
            async with self._client.messages.stream(**self.build_kwargs(request)) as stream:
                async for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "text":
                        yield StreamChunk(kind="text", delta=event.text)
                    elif etype == "content_block_delta":
                        delta = event.delta
                        if getattr(delta, "type", "") == "thinking_delta":
                            yield StreamChunk(kind="thinking", delta=delta.thinking)
                final = await stream.get_final_message()
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        response = self._decode(final, self.name)
        for call in response.tool_calls:
            yield StreamChunk(kind="tool_call", tool_call=call)
        yield StreamChunk(kind="done", response=response)

    async def structured(self, request: ChatRequest, schema: type[T]) -> tuple[T, Usage]:
        """Native structured output; the result is re-validated."""
        kwargs = self.build_kwargs(
            request.model_copy(update={"tools": [], "allow_thinking": False})
        )
        config = dict(kwargs.get("output_config", {}))
        config["format"] = {"type": "json_schema", "schema": to_strict_schema(schema)}
        kwargs["output_config"] = config
        kwargs.pop("thinking", None)
        try:
            message = await self._client.messages.create(**kwargs)
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        response = self._decode(message, self.name)
        if response.stop_reason == "refusal":
            raise StructuredOutputError("model refused the structured request")
        return validate_structured(schema, response.text), response.usage
