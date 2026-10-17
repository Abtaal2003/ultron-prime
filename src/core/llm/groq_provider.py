"""Groq adapter (spec 7.1): fast guard, summariser, and reader models.

Groq speaks the OpenAI chat-completions dialect. Strict structured outputs (constrained decoding)
exist on a subset of models and cannot be combined with streaming or tools; other models fall
back to JSON mode with the schema in the prompt and a Pydantic re-validation.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

from src.core.errors import ProviderUnavailable, StructuredOutputError
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
    images_as_text,
)
from src.core.llm.errors_map import from_status
from src.core.llm.structured import to_strict_schema, validate_structured

_STRICT_MODELS = ("gpt-oss", "kimi-k2")
_REASONING_MODELS = ("gpt-oss",)
_VISION_MODELS = ("llama-4", "scout", "maverick")
_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class GroqProvider:
    """Adapter over ``groq.AsyncGroq``."""

    name = "groq"

    def __init__(self, api_key: str, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            from groq import AsyncGroq

            self._client = AsyncGroq(api_key=api_key, max_retries=0)

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Capabilities by model family; unknown models get the conservative defaults."""
        strict = any(tag in model for tag in _STRICT_MODELS)
        return ProviderCapabilities(
            tool_calling=True,
            forced_tool_choice=True,
            parallel_tool_calls=True,
            streaming=True,
            structured_output="native_strict" if strict else "json_mode",
            structured_output_with_tools=False,
            supports_temperature=True,
            supports_effort=any(tag in model for tag in _REASONING_MODELS),
            thinking_portable_to=frozenset(),
            prompt_caching=strict,
            vision=any(tag in model for tag in _VISION_MODELS),
            pdf_input=False,
            max_image_bytes=4 * 1024 * 1024,
            max_context=131_072,
        )

    # --------------------------------------------------------------------------- encoding --
    def _encode_messages(self, request: ChatRequest, vision: bool) -> list[dict[str, Any]]:
        messages = request.messages if vision else images_as_text(request.messages)
        out: list[dict[str, Any]] = []
        system = "\n\n".join(p for p in (request.system, request.system_dynamic) if p)
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "user":
                out.extend(self._encode_user(m))
            else:
                out.append(self._encode_assistant(m))
        return out

    @staticmethod
    def _encode_user(message: Message) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        content: list[dict[str, Any]] = []
        for p in message.parts:
            if isinstance(p, ToolResultPart):
                result.append({"role": "tool", "tool_call_id": p.tool_use_id, "content": p.content})
            elif isinstance(p, TextPart) and p.text:
                content.append({"type": "text", "text": p.text})
            elif isinstance(p, ImagePart):
                b64 = base64.b64encode(p.data).decode()
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{p.media_type};base64,{b64}"}}
                )
            elif isinstance(p, DocumentPart):
                content.append(
                    {"type": "text", "text": f"[document: {p.description or 'omitted'}]"}
                )
        if content:
            if all(c["type"] == "text" for c in content):
                result.append({"role": "user", "content": "\n".join(c["text"] for c in content)})
            else:
                result.append({"role": "user", "content": content})
        return result

    @staticmethod
    def _encode_assistant(message: Message) -> dict[str, Any]:
        text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
        calls = [
            {
                "id": p.id,
                "type": "function",
                "function": {"name": p.name, "arguments": json.dumps(p.arguments)},
            }
            for p in message.parts
            if isinstance(p, ToolUsePart)
        ]
        encoded: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            encoded["tool_calls"] = calls
        return encoded

    def build_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        """Translate ``request`` into ``chat.completions.create`` keyword arguments."""
        caps = self.capabilities(request.model)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": self._encode_messages(request, caps.vision),
            "max_completion_tokens": request.max_tokens,
            "timeout": request.timeout_s,
        }
        if caps.supports_temperature and request.sampling.temperature is not None:
            kwargs["temperature"] = request.sampling.temperature
        if caps.supports_effort:
            effort = request.sampling.effort or "low"
            kwargs["reasoning_effort"] = {"low": "low", "medium": "medium"}.get(effort, "high")
            kwargs["include_reasoning"] = False
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
            kwargs["tool_choice"] = request.tool_choice
        return kwargs

    # --------------------------------------------------------------------------- decoding --
    @staticmethod
    def _usage(raw: Any) -> Usage:
        if raw is None:
            return Usage()
        details = getattr(raw, "prompt_tokens_details", None)
        return Usage(
            input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw, "completion_tokens", 0) or 0,
            cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
        )

    def _decode(self, completion: Any) -> ChatResponse:
        choice = completion.choices[0]
        msg = choice.message
        parts: list[Any] = []
        if getattr(msg, "content", None):
            parts.append(TextPart(text=msg.content))
        for call in getattr(msg, "tool_calls", None) or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": call.function.arguments[:500]}
            parts.append(ToolUsePart(id=call.id, name=call.function.name, arguments=dict(args)))
        reason = _STOP.get(str(choice.finish_reason), "other")
        if any(isinstance(p, ToolUsePart) for p in parts):
            reason = "tool_use"
        return ChatResponse(
            provider=self.name,
            model=getattr(completion, "model", ""),
            message=Message(role="assistant", parts=parts),
            stop_reason=reason,
            usage=self._usage(getattr(completion, "usage", None)),
        )

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        import groq

        if isinstance(exc, groq.APIStatusError):
            headers = getattr(getattr(exc, "response", None), "headers", None)
            return from_status(exc.status_code, str(exc), headers)
        if isinstance(exc, groq.APITimeoutError | groq.APIConnectionError):
            return from_status(None, type(exc).__name__)
        return exc

    # ------------------------------------------------------------------------------ calls --
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Complete a request."""
        try:
            completion = await self._client.chat.completions.create(**self.build_kwargs(request))
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return self._decode(completion)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream text; tool-call fragments are assembled and emitted whole at the end."""
        kwargs = self.build_kwargs(request)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish = "stop"
        usage = Usage()
        model = request.model
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                model = getattr(chunk, "model", model) or model
                if getattr(chunk, "usage", None) is not None:
                    usage = self._usage(chunk.usage)
                xg = getattr(chunk, "x_groq", None)
                if xg is not None and getattr(xg, "usage", None) is not None:
                    usage = self._usage(xg.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    text.append(delta.content)
                    yield StreamChunk(kind="text", delta=delta.content)
                for frag in getattr(delta, "tool_calls", None) or []:
                    slot = calls.setdefault(frag.index, {"id": "", "name": "", "args": ""})
                    slot["id"] = frag.id or slot["id"]
                    if frag.function is not None:
                        slot["name"] = frag.function.name or slot["name"]
                        slot["args"] += frag.function.arguments or ""
                if choice.finish_reason:
                    finish = str(choice.finish_reason)
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        parts: list[Any] = []
        if text:
            parts.append(TextPart(text="".join(text)))
        for _, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": slot["args"][:500]}
            call = ToolUsePart(
                id=slot["id"] or f"call_{len(parts)}", name=slot["name"], arguments=args
            )
            parts.append(call)
            yield StreamChunk(kind="tool_call", tool_call=call)
        reason = "tool_use" if calls else _STOP.get(finish, "other")
        yield StreamChunk(
            kind="done",
            response=ChatResponse(
                provider=self.name,
                model=model,
                message=Message(role="assistant", parts=parts),
                stop_reason=reason,
                usage=usage,
            ),
        )

    async def structured(self, request: ChatRequest, schema: type[T]) -> tuple[T, Usage]:
        """Strict constrained decoding where supported, JSON mode otherwise."""
        caps = self.capabilities(request.model)
        json_schema = to_strict_schema(schema)
        plain = request.model_copy(update={"tools": []})
        if caps.structured_output == "native_strict":
            kwargs = self.build_kwargs(plain)
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": json_schema},
            }
        else:
            hint = (
                "Reply with a single JSON object that matches this JSON Schema, and nothing else:\n"
                + json.dumps(json_schema)
            )
            plain = plain.model_copy(
                update={"system_dynamic": f"{request.system_dynamic}\n\n{hint}".strip()}
            )
            kwargs = self.build_kwargs(plain)
            kwargs["response_format"] = {"type": "json_object"}
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            mapped = self._map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        response = self._decode(completion)
        if response.stop_reason == "max_tokens":
            raise ProviderUnavailable("structured output was cut off")
        if not response.text:
            raise StructuredOutputError("model returned no structured output")
        return validate_structured(schema, response.text), response.usage
