"""Provider-neutral LLM types and the provider protocol (spec 7.2).

Messages are lists of typed parts. Each adapter converts to and from its SDK's wire format, so
the agent, the guards, and the tests never touch an SDK type.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
StructuredMode = Literal["native_strict", "native_best_effort", "json_mode", "none"]


class ProviderCapabilities(BaseModel):
    """What one model can do (checked at startup and shown in Settings)."""

    tool_calling: bool = True
    forced_tool_choice: bool = True
    parallel_tool_calls: bool = True
    streaming: bool = True
    structured_output: StructuredMode = "native_best_effort"
    structured_output_with_tools: bool = False
    supports_temperature: bool = True
    supports_effort: bool = False
    thinking_portable_to: frozenset[str] = frozenset()
    prompt_caching: bool = False
    vision: bool = False
    pdf_input: bool = False
    max_image_bytes: int | None = None
    max_context: int = 128_000


# --- message parts --------------------------------------------------------------------------


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextPart(_Part):
    """Plain text."""

    kind: Literal["text"] = "text"
    text: str


class ImagePart(_Part):
    """An image (PNG, JPEG, WebP, or GIF)."""

    kind: Literal["image"] = "image"
    media_type: str
    data: bytes
    description: str | None = Field(None, description="Stored text used if a model lacks vision.")


class DocumentPart(_Part):
    """A PDF handed to a model that reads PDFs natively."""

    kind: Literal["document"] = "document"
    media_type: str = "application/pdf"
    data: bytes
    description: str | None = None


class ThinkingPart(_Part):
    """A model's reasoning block. Only valid for the family that produced it."""

    kind: Literal["thinking"] = "thinking"
    text: str = ""
    signature: str | None = None
    family: str = ""
    redacted: bool = False


class ToolUsePart(_Part):
    """A tool call requested by the model."""

    kind: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None  # Gemini thought signature, passed back verbatim


class ToolResultPart(_Part):
    """The result of a tool call, as text."""

    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    name: str = ""
    content: str
    is_error: bool = False


Part = Annotated[
    TextPart | ImagePart | DocumentPart | ThinkingPart | ToolUsePart | ToolResultPart,
    Field(discriminator="kind"),
]


class Message(BaseModel):
    """One conversation turn made of parts."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    parts: list[Part]

    @classmethod
    def user(cls, text: str) -> Message:
        """Build a user text message."""
        return cls(role="user", parts=[TextPart(text=text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        """Build an assistant text message."""
        return cls(role="assistant", parts=[TextPart(text=text)])

    def text(self) -> str:
        """Concatenate the text parts."""
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


class ToolSpec(BaseModel):
    """A tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]


class SamplingPolicy(BaseModel):
    """Per-call sampling (spec 7.5). Adapters drop what a model does not support."""

    effort: Effort | None = None
    temperature: float | None = None


ToolChoice = Literal["auto", "none", "required"]


class ChatRequest(BaseModel):
    """A provider-neutral request. ``model`` is set by the router."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = ""
    system: str = ""
    # Per-turn system text placed *after* the cache breakpoint (anchors, memory, session...).
    system_dynamic: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: ToolChoice = "auto"
    sampling: SamplingPolicy = Field(default_factory=SamplingPolicy)
    max_tokens: int = Field(4096, ge=16)
    timeout_s: float = Field(60.0, gt=0)
    cache_system: bool = True
    role: str = ""
    allow_thinking: bool = True


class Usage(BaseModel):
    """Token counts for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        """Return the sum of two usages."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


class ChatResponse(BaseModel):
    """A complete model reply."""

    provider: str
    model: str
    message: Message
    stop_reason: StopReason = "end_turn"
    usage: Usage = Field(default_factory=Usage)

    @property
    def text(self) -> str:
        """The reply text."""
        return self.message.text()

    @property
    def tool_calls(self) -> list[ToolUsePart]:
        """Tool calls requested in this reply."""
        return [p for p in self.message.parts if isinstance(p, ToolUsePart)]


class StreamChunk(BaseModel):
    """One incremental piece of a streamed reply."""

    kind: Literal["text", "thinking", "tool_call", "done"]
    delta: str = ""
    tool_call: ToolUsePart | None = None
    response: ChatResponse | None = None


class LLMProvider(Protocol):
    """What the router needs from an adapter."""

    name: str

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Return the capabilities of ``model``."""
        ...

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Complete a request."""
        ...

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream a reply; the final chunk is ``done`` with the full response."""
        ...

    async def structured(self, request: ChatRequest, schema: type[T]) -> tuple[T, Usage]:
        """Return a validated instance of ``schema``."""
        ...


def strip_thinking(messages: list[Message], keep_family: str | None = None) -> list[Message]:
    """Drop thinking parts that ``keep_family`` cannot read (all of them when it is ``None``)."""
    out: list[Message] = []
    for m in messages:
        parts = [
            p
            for p in m.parts
            if not isinstance(p, ThinkingPart) or (keep_family and p.family == keep_family)
        ]
        if parts:
            out.append(m.model_copy(update={"parts": parts}))
    return out


def has_images(messages: list[Message]) -> bool:
    """Whether any message carries an image or PDF part."""
    return any(isinstance(p, ImagePart | DocumentPart) for m in messages for p in m.parts)


def images_as_text(messages: list[Message]) -> list[Message]:
    """Replace image and document parts with their stored text descriptions."""
    out: list[Message] = []
    for m in messages:
        parts: list[Any] = []
        for p in m.parts:
            if isinstance(p, ImagePart | DocumentPart):
                label = "image" if isinstance(p, ImagePart) else "document"
                parts.append(
                    TextPart(text=f"[{label}: {p.description or 'not shown to this model'}]")
                )
            else:
                parts.append(p)
        out.append(m.model_copy(update={"parts": parts}))
    return out
