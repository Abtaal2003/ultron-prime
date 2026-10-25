"""Google Gemini adapter (spec 7.1) built on ``google-genai``.

Gemini 3.x models take a ``thinking_level``. Their reasoning is not portable, so thoughts are
never surfaced. A function call's ``thought_signature`` must be sent back verbatim on the next
turn, so it is kept on the :class:`ToolUsePart`.
"""

from __future__ import annotations

import base64
import uuid
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
    ToolResultPart,
    ToolUsePart,
    Usage,
)
from src.core.llm.errors_map import from_status
from src.core.llm.structured import to_strict_schema, validate_structured

_STOP: dict[str, StopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "RECITATION": "refusal",
}
_MIN_DEADLINE_S = 10.0
"""Gemini rejects a request deadline under ten seconds (found in the first live run)."""
_LEVEL = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}


FOREIGN_SIGNATURE = b"skip_thought_signature_validator"
"""Sent for a function call that Gemini didn't make (one from another provider after a
failover). Gemini 3 rejects an unsigned call in the history with a 400; this documented dummy
value tells it to skip the check. A live run (2026-10-25) failed a turn on exactly that."""


class GeminiProvider:
    """Adapter over ``google.genai.Client`` (async surface)."""

    name = "gemini"

    def __init__(self, api_key: str, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            from google import genai

            self._client = genai.Client(api_key=api_key)

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Gemini 3.x: tools, vision, PDFs, native schema output, long context."""
        return ProviderCapabilities(
            tool_calling=True,
            forced_tool_choice=True,
            parallel_tool_calls=True,
            streaming=True,
            structured_output="native_best_effort",
            structured_output_with_tools=False,
            supports_temperature=True,
            supports_effort=False,
            thinking_portable_to=frozenset(),
            prompt_caching=False,
            vision=True,
            pdf_input=True,
            max_image_bytes=7 * 1024 * 1024,
            max_context=1_000_000,
        )

    # --------------------------------------------------------------------------- encoding --
    @staticmethod
    def _encode_messages(messages: list[Message]) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for m in messages:
            parts: list[Any] = []
            for p in m.parts:
                if isinstance(p, TextPart) and p.text:
                    parts.append(types.Part(text=p.text))
                elif isinstance(p, ImagePart | DocumentPart):
                    parts.append(types.Part.from_bytes(data=p.data, mime_type=p.media_type))
                elif isinstance(p, ToolUsePart):
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(name=p.name, args=p.arguments),
                            thought_signature=base64.b64decode(p.signature)
                            if p.signature
                            else FOREIGN_SIGNATURE,
                        )
                    )
                elif isinstance(p, ToolResultPart):
                    key = "error" if p.is_error else "result"
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=p.name or "tool", response={key: p.content}
                            )
                        )
                    )
            if parts:
                contents.append(
                    types.Content(role="user" if m.role == "user" else "model", parts=parts)
                )
        return contents

    def build_config(self, request: ChatRequest) -> Any:
        """Build a ``GenerateContentConfig`` from ``request``."""
        from google.genai import types

        kwargs: dict[str, Any] = {
            "max_output_tokens": request.max_tokens,
            "http_options": types.HttpOptions(
                timeout=int(max(request.timeout_s, _MIN_DEADLINE_S) * 1000)
            ),
        }
        system = "\n\n".join(p for p in (request.system, request.system_dynamic) if p)
        if system:
            kwargs["system_instruction"] = system
        if request.sampling.temperature is not None:
            kwargs["temperature"] = request.sampling.temperature
        level = _LEVEL.get(request.sampling.effort or "")
        if level and request.allow_thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=level, include_thoughts=False
            )
        if request.tools:
            kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in request.tools
                    ]
                )
            ]
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}[request.tool_choice]
            kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=mode)
            )
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )
        return types.GenerateContentConfig(**kwargs)

    # --------------------------------------------------------------------------- decoding --
    def _decode(self, response: Any, model: str) -> ChatResponse:
        parts: list[Any] = []
        finish = "STOP"
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            cand = candidates[0]
            finish = str(
                getattr(getattr(cand, "finish_reason", None), "name", cand.finish_reason or "STOP")
            )
            for part in getattr(getattr(cand, "content", None), "parts", None) or []:
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    parts.append(TextPart(text=part.text))
                call = getattr(part, "function_call", None)
                if call is not None and call.name:
                    sig = getattr(part, "thought_signature", None)
                    parts.append(
                        ToolUsePart(
                            id=getattr(call, "id", None) or f"call_{uuid.uuid4().hex[:12]}",
                            name=call.name,
                            arguments=dict(call.args or {}),
                            signature=base64.b64encode(sig).decode() if sig else None,
                        )
                    )
        elif getattr(getattr(response, "prompt_feedback", None), "block_reason", None):
            finish = "SAFETY"
        meta = getattr(response, "usage_metadata", None)
        out_tokens = getattr(meta, "response_token_count", None) or getattr(
            meta, "candidates_token_count", 0
        )
        reason = _STOP.get(finish, "other")
        if any(isinstance(p, ToolUsePart) for p in parts):
            reason = "tool_use"
        return ChatResponse(
            provider=self.name,
            model=getattr(response, "model_version", None) or model,
            message=Message(role="assistant", parts=parts),
            stop_reason=reason,
            usage=Usage(
                input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
                output_tokens=(out_tokens or 0) + (getattr(meta, "thoughts_token_count", 0) or 0),
                cache_read_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            ),
        )

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            return from_status(getattr(exc, "code", None), str(exc))
        if isinstance(exc, TimeoutError | ConnectionError):
            return from_status(None, type(exc).__name__)
        try:
            import httpx

            if isinstance(exc, httpx.TransportError):
                return from_status(None, type(exc).__name__)
        except ImportError:  # pragma: no cover
            pass
        return exc

    # ------------------------------------------------------------------------------ calls --
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Complete a request."""
        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=self._encode_messages(request.messages),
                config=self.build_config(request),
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return self._decode(response, request.model)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream text; tool calls are emitted as they appear."""
        text: list[str] = []
        tool_parts: list[ToolUsePart] = []
        last: Any = None
        usage = Usage()
        finish = "end_turn"
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=request.model,
                contents=self._encode_messages(request.messages),
                config=self.build_config(request),
            )
            async for chunk in stream:
                last = chunk
                decoded = self._decode(chunk, request.model)
                if decoded.usage.input_tokens or decoded.usage.output_tokens:
                    usage = decoded.usage
                if decoded.stop_reason != "end_turn" or decoded.message.parts:
                    finish = decoded.stop_reason if decoded.stop_reason != "other" else finish
                for part in decoded.message.parts:
                    if isinstance(part, TextPart):
                        text.append(part.text)
                        yield StreamChunk(kind="text", delta=part.text)
                    elif isinstance(part, ToolUsePart):
                        tool_parts.append(part)
                        yield StreamChunk(kind="tool_call", tool_call=part)
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        parts: list[Any] = []
        if text:
            parts.append(TextPart(text="".join(text)))
        parts.extend(tool_parts)
        yield StreamChunk(
            kind="done",
            response=ChatResponse(
                provider=self.name,
                model=getattr(last, "model_version", None) or request.model,
                message=Message(role="assistant", parts=parts),
                stop_reason="tool_use" if tool_parts else finish,
                usage=usage,
            ),
        )

    async def structured(self, request: ChatRequest, schema: type[T]) -> tuple[T, Usage]:
        """Constrain the reply to ``schema`` and re-validate it."""
        plain = request.model_copy(update={"tools": [], "allow_thinking": False})
        config = self.build_config(plain)
        config.response_mime_type = "application/json"
        config.response_json_schema = to_strict_schema(schema)
        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=self._encode_messages(request.messages),
                config=config,
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        decoded = self._decode(response, request.model)
        if not decoded.text:
            raise StructuredOutputError("model returned no structured output")
        return validate_structured(schema, decoded.text), decoded.usage
