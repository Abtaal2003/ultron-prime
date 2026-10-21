"""M5: provider adapters (against fake SDK clients) and the router."""

from __future__ import annotations

import base64
from types import SimpleNamespace as NS
from typing import Any

import anthropic
import groq
import httpx
import pytest
from pydantic import BaseModel
from src.config import Settings
from src.core.db import Database
from src.core.errors import (
    ConfigurationError,
    ProviderRejected,
    ProviderUnavailable,
    RateLimited,
    StructuredOutputError,
)
from src.core.events import AgentEvent, ProviderFailover
from src.core.llm.anthropic_provider import AnthropicProvider
from src.core.llm.base import (
    ChatRequest,
    DocumentPart,
    ImagePart,
    Message,
    ProviderCapabilities,
    SamplingPolicy,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
    has_images,
    images_as_text,
    strip_thinking,
)
from src.core.llm.errors_map import from_status, retry_after_seconds
from src.core.llm.fake import FakeProvider, Reply, failing
from src.core.llm.gemini_provider import GeminiProvider
from src.core.llm.groq_provider import GroqProvider
from src.core.llm.router import CallContext, LLMRouter

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class Verdict(BaseModel):
    """Test schema."""

    label: str
    score: int


def _req(text: str = "hi", **kw: Any) -> ChatRequest:
    return ChatRequest(model=kw.pop("model", "m"), messages=[Message.user(text)], **kw)


def _http_error(
    cls: type[Exception], status: int, headers: dict[str, str] | None = None
) -> Exception:
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return cls("boom", response=resp, body=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------- shared helpers ----------------
def test_retry_after_parsing() -> None:
    assert retry_after_seconds({"retry-after": "3"}) == 3.0
    assert retry_after_seconds({"Retry-After": "9999"}) == 120.0
    assert retry_after_seconds({"retry-after": "soon"}) is None
    assert retry_after_seconds(None) is None


def test_status_classification() -> None:
    assert isinstance(from_status(429, "x", {"retry-after": "2"}), RateLimited)
    assert isinstance(from_status(503, "x"), ProviderUnavailable)
    assert isinstance(from_status(None, "x"), ProviderUnavailable)
    rejected = from_status(401, "x")
    assert isinstance(rejected, ProviderRejected)
    assert rejected.retryable is False


def test_message_helpers() -> None:
    thinking = ThinkingPart(text="t", family="claude", signature="s")
    msgs = [
        Message(role="assistant", parts=[thinking, TextPart(text="a")]),
        Message(
            role="user", parts=[ImagePart(media_type="image/png", data=PNG, description="a cat")]
        ),
    ]
    assert has_images(msgs)
    assert strip_thinking(msgs, None)[0].parts == [TextPart(text="a")]
    assert strip_thinking(msgs, "claude")[0].parts[0] == thinking
    text = images_as_text(msgs)[1].parts[0]
    assert isinstance(text, TextPart)
    assert "a cat" in text.text
    assert not has_images(images_as_text(msgs))


# ---------------------------------------------------------------- anthropic --------------------
class FakeAnthropic:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self._reply = reply
        self._error = error

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._error:
            raise self._error
        return self._reply


def _anthropic_reply(
    text: str = "hello", stop: str = "end_turn", extra: list[Any] | None = None
) -> Any:
    return NS(
        model="claude-sonnet-5-5",
        stop_reason=stop,
        content=[NS(type="text", text=text), *(extra or [])],
        usage=NS(
            input_tokens=11,
            output_tokens=7,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
        ),
    )


async def test_anthropic_request_shape_for_5x_models() -> None:
    fake = FakeAnthropic(_anthropic_reply())
    provider = AnthropicProvider("k", client=fake)
    tool = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}})
    req = _req(
        model="claude-sonnet-5-5",
        system="SYS",
        tools=[tool],
        tool_choice="required",
        sampling=SamplingPolicy(effort="high", temperature=0.9),
    )
    resp = await provider.chat(req)
    kw = fake.calls[0]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["thinking"] == {"type": "adaptive"}
    assert "temperature" not in kw
    assert "tool_choice" not in kw  # forced tool use unsupported on 5.5
    assert kw["tools"][0]["input_schema"]["type"] == "object"
    assert resp.text == "hello"
    assert resp.usage.cache_read_tokens == 3
    assert resp.usage.cache_write_tokens == 2


async def test_anthropic_low_effort_has_no_thinking_and_none_choice() -> None:
    fake = FakeAnthropic(_anthropic_reply())
    provider = AnthropicProvider("k", client=fake)
    tool = ToolSpec(name="t", description="d", parameters={"type": "object"})
    await provider.chat(
        _req(
            model="claude-sonnet-5-5",
            tools=[tool],
            tool_choice="none",
            sampling=SamplingPolicy(effort="low"),
        )
    )
    kw = fake.calls[0]
    assert "thinking" not in kw
    assert kw["tool_choice"] == {"type": "none"}


async def test_anthropic_legacy_model_uses_temperature() -> None:
    fake = FakeAnthropic(_anthropic_reply())
    provider = AnthropicProvider("k", client=fake)
    assert provider.capabilities("claude-sonnet-4-5").supports_temperature
    await provider.chat(
        _req(model="claude-sonnet-4-5", sampling=SamplingPolicy(effort="high", temperature=0.3))
    )
    kw = fake.calls[0]
    assert kw["temperature"] == 0.3
    assert "output_config" not in kw


async def test_anthropic_encodes_media_tools_and_drops_foreign_thinking() -> None:
    fake = FakeAnthropic(_anthropic_reply())
    provider = AnthropicProvider("k", client=fake)
    messages = [
        Message(
            role="user",
            parts=[
                TextPart(text="look"),
                ImagePart(media_type="image/png", data=PNG),
                DocumentPart(data=b"%PDF-1.4"),
            ],
        ),
        Message(
            role="assistant",
            parts=[
                ThinkingPart(text="mine", signature="sig", family="claude"),
                ThinkingPart(text="foreign", family="gemini"),
                ToolUsePart(id="c1", name="t", arguments={"a": 1}),
            ],
        ),
        Message(role="user", parts=[ToolResultPart(tool_use_id="c1", name="t", content="done")]),
    ]
    await provider.chat(ChatRequest(model="claude-sonnet-5-5", messages=messages))
    sent = fake.calls[0]["messages"]
    assert sent[0]["content"][1]["source"]["data"] == base64.b64encode(PNG).decode()
    assert sent[0]["content"][2]["type"] == "document"
    kinds = [b["type"] for b in sent[1]["content"]]
    assert kinds == ["thinking", "tool_use"]
    assert sent[2]["content"][0]["tool_use_id"] == "c1"


async def test_anthropic_decodes_tool_use_and_thinking() -> None:
    reply = _anthropic_reply(
        stop="tool_use",
        extra=[
            NS(type="thinking", thinking="hmm", signature="sg"),
            NS(type="tool_use", id="tu1", name="web", input={"q": "x"}),
        ],
    )
    resp = await AnthropicProvider("k", client=FakeAnthropic(reply)).chat(
        _req(model="claude-sonnet-5-5")
    )
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].arguments == {"q": "x"}
    thinking = [p for p in resp.message.parts if isinstance(p, ThinkingPart)]
    assert thinking[0].family == "claude"
    assert thinking[0].signature == "sg"


async def test_anthropic_structured_validates_and_rejects_refusal() -> None:
    ok = FakeAnthropic(_anthropic_reply('{"label": "a", "score": 2}'))
    value, usage = await AnthropicProvider("k", client=ok).structured(
        _req(model="claude-sonnet-5-5"), Verdict
    )
    assert value == Verdict(label="a", score=2)
    assert usage.input_tokens == 11
    fmt = ok.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert "thinking" not in ok.calls[0]
    with pytest.raises(StructuredOutputError):
        await AnthropicProvider("k", client=FakeAnthropic(_anthropic_reply("not json"))).structured(
            _req(model="claude-sonnet-5-5"), Verdict
        )
    with pytest.raises(StructuredOutputError, match="refused"):
        await AnthropicProvider(
            "k", client=FakeAnthropic(_anthropic_reply("", stop="refusal"))
        ).structured(_req(model="claude-sonnet-5-5"), Verdict)


async def test_anthropic_error_mapping() -> None:
    limited = AnthropicProvider(
        "k",
        client=FakeAnthropic(
            error=_http_error(anthropic.RateLimitError, 429, {"retry-after": "4"})
        ),
    )
    with pytest.raises(RateLimited) as info:
        await limited.chat(_req(model="claude-sonnet-5-5"))
    assert info.value.retry_after_s == 4.0
    bad_key = AnthropicProvider(
        "k", client=FakeAnthropic(error=_http_error(anthropic.AuthenticationError, 401))
    )
    with pytest.raises(ProviderRejected):
        await bad_key.chat(_req(model="claude-sonnet-5-5"))
    down = AnthropicProvider(
        "k", client=FakeAnthropic(error=_http_error(anthropic.InternalServerError, 500))
    )
    with pytest.raises(ProviderUnavailable):
        await down.chat(_req(model="claude-sonnet-5-5"))
    boom = AnthropicProvider("k", client=FakeAnthropic(error=ValueError("bug")))
    with pytest.raises(ValueError, match="bug"):
        await boom.chat(_req(model="claude-sonnet-5-5"))


class FakeStreamCtx:
    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> FakeStreamCtx:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for e in self._events:
                yield e

        return gen()

    async def get_final_message(self) -> Any:
        return self._final


async def test_anthropic_stream() -> None:
    events = [
        NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="hm")),
        NS(type="text", text="Hel", snapshot="Hel"),
        NS(type="text", text="lo", snapshot="Hello"),
    ]
    final = _anthropic_reply("Hello", extra=[NS(type="tool_use", id="t", name="n", input={})])
    client = NS(messages=NS(stream=lambda **kw: FakeStreamCtx(events, final)))
    chunks = [
        c
        async for c in AnthropicProvider("k", client=client).stream(_req(model="claude-sonnet-5-5"))
    ]
    assert [c.kind for c in chunks] == ["thinking", "text", "text", "tool_call", "done"]
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "Hello"


# ---------------------------------------------------------------- groq ------------------------
class FakeGroq:
    def __init__(
        self, reply: Any = None, error: Exception | None = None, stream: list[Any] | None = None
    ):
        self.calls: list[dict[str, Any]] = []
        self.chat = NS(completions=self)
        self._reply, self._error, self._stream = reply, error, stream

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._error:
            raise self._error
        if kw.get("stream"):
            stream = self._stream or []

            async def gen() -> Any:
                for c in stream:
                    yield c

            return gen()
        return self._reply


def _groq_reply(
    text: str | None = "yo", calls: list[Any] | None = None, finish: str = "stop"
) -> Any:
    return NS(
        model="openai/gpt-oss-20b",
        choices=[NS(message=NS(content=text, tool_calls=calls), finish_reason=finish)],
        usage=NS(prompt_tokens=9, completion_tokens=4, prompt_tokens_details=NS(cached_tokens=5)),
    )


async def test_groq_request_encoding() -> None:
    fake = FakeGroq(_groq_reply())
    provider = GroqProvider("k", client=fake)
    messages = [
        Message.user("hello"),
        Message(
            role="assistant",
            parts=[TextPart(text="calling"), ToolUsePart(id="c1", name="t", arguments={"a": 1})],
        ),
        Message(
            role="user",
            parts=[
                ToolResultPart(tool_use_id="c1", name="t", content="res"),
                ImagePart(media_type="image/png", data=PNG, description="a dog"),
            ],
        ),
    ]
    tool = ToolSpec(name="t", description="d", parameters={"type": "object"})
    await provider.chat(
        ChatRequest(
            model="openai/gpt-oss-20b",
            system="SYS",
            messages=messages,
            tools=[tool],
            sampling=SamplingPolicy(effort="xhigh", temperature=0.2),
        )
    )
    kw = fake.calls[0]
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert kw["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert "a dog" in kw["messages"][4]["content"]  # gpt-oss has no vision: image became text
    assert kw["reasoning_effort"] == "high"
    assert kw["include_reasoning"] is False
    assert kw["temperature"] == 0.2
    assert kw["tool_choice"] == "auto"


async def test_groq_vision_model_receives_image_url() -> None:
    fake = FakeGroq(_groq_reply())
    provider = GroqProvider("k", client=fake)
    msg = Message(
        role="user", parts=[TextPart(text="see"), ImagePart(media_type="image/png", data=PNG)]
    )
    await provider.chat(ChatRequest(model="meta-llama/llama-4-scout-17b", messages=[msg]))
    content = fake.calls[0]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_groq_decode_tool_calls_and_bad_json() -> None:
    calls = [
        NS(id="c1", function=NS(name="a", arguments='{"x": 1}')),
        NS(id="c2", function=NS(name="b", arguments="{oops")),
    ]
    resp = await GroqProvider("k", client=FakeGroq(_groq_reply(None, calls, "tool_calls"))).chat(
        _req(model="openai/gpt-oss-20b")
    )
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].arguments == {"x": 1}
    assert "_unparseable" in resp.tool_calls[1].arguments
    assert resp.usage.cache_read_tokens == 5


async def test_groq_structured_strict_vs_json_mode() -> None:
    strict = FakeGroq(_groq_reply('{"label": "x", "score": 1}'))
    value, _ = await GroqProvider("k", client=strict).structured(
        _req(model="openai/gpt-oss-20b"), Verdict
    )
    assert value.score == 1
    rf = strict.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    plain = FakeGroq(_groq_reply('{"label": "x", "score": 1}'))
    await GroqProvider("k", client=plain).structured(
        _req(model="llama-3.3-70b-versatile", system="S"), Verdict
    )
    assert plain.calls[0]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in plain.calls[0]["messages"][0]["content"]
    with pytest.raises(StructuredOutputError):
        await GroqProvider("k", client=FakeGroq(_groq_reply("nope"))).structured(
            _req(model="openai/gpt-oss-20b"), Verdict
        )
    with pytest.raises(StructuredOutputError):
        await GroqProvider("k", client=FakeGroq(_groq_reply(None))).structured(
            _req(model="openai/gpt-oss-20b"), Verdict
        )
    with pytest.raises(ProviderUnavailable):
        await GroqProvider("k", client=FakeGroq(_groq_reply("{", finish="length"))).structured(
            _req(model="openai/gpt-oss-20b"), Verdict
        )


async def test_groq_stream_assembles_tool_calls() -> None:
    def chunk(
        content: str | None = None, tool: Any = None, finish: str | None = None, usage: Any = None
    ) -> Any:
        delta = NS(content=content, tool_calls=[tool] if tool else None)
        return NS(
            model="m", choices=[NS(delta=delta, finish_reason=finish)], usage=usage, x_groq=None
        )

    stream = [
        chunk("He"),
        chunk("llo"),
        chunk(tool=NS(index=0, id="c1", function=NS(name="web", arguments='{"q":'))),
        chunk(
            tool=NS(index=0, id=None, function=NS(name=None, arguments=' "x"}')),
            finish="tool_calls",
        ),
        NS(
            model="m",
            choices=[],
            usage=NS(prompt_tokens=3, completion_tokens=2, prompt_tokens_details=None),
            x_groq=None,
        ),
    ]
    provider = GroqProvider("k", client=FakeGroq(stream=stream))
    out = [c async for c in provider.stream(_req(model="openai/gpt-oss-20b"))]
    assert [c.kind for c in out] == ["text", "text", "tool_call", "done"]
    done = out[-1].response
    assert done is not None
    assert done.text == "Hello"
    assert done.tool_calls[0].arguments == {"q": "x"}
    assert done.stop_reason == "tool_use"
    assert done.usage.input_tokens == 3


async def test_groq_error_mapping() -> None:
    provider = GroqProvider("k", client=FakeGroq(error=_http_error(groq.RateLimitError, 429)))
    with pytest.raises(RateLimited):
        await provider.chat(_req(model="openai/gpt-oss-20b"))
    provider = GroqProvider("k", client=FakeGroq(error=_http_error(groq.BadRequestError, 400)))
    with pytest.raises(ProviderRejected):
        await provider.chat(_req(model="openai/gpt-oss-20b"))


# ---------------------------------------------------------------- gemini ----------------------
class FakeGemini:
    def __init__(
        self, reply: Any = None, error: Exception | None = None, stream: list[Any] | None = None
    ):
        self.calls: list[dict[str, Any]] = []
        self.aio = NS(models=self)
        self._reply, self._error, self._stream = reply, error, stream

    async def generate_content(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._error:
            raise self._error
        return self._reply

    async def generate_content_stream(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._error:
            raise self._error
        items = self._stream or []

        async def gen() -> Any:
            for i in items:
                yield i

        return gen()


def _gem_reply(text: str = "hey", call: Any = None, finish: str = "STOP") -> Any:
    parts = []
    if text:
        parts.append(NS(text=text, thought=False, function_call=None, thought_signature=None))
    if call:
        parts.append(
            NS(text=None, thought=False, function_call=call, thought_signature=b"\x01\x02")
        )
    parts.append(
        NS(text="private thought", thought=True, function_call=None, thought_signature=None)
    )
    return NS(
        model_version="gemini-3.5-flash",
        candidates=[NS(finish_reason=NS(name=finish), content=NS(parts=parts))],
        usage_metadata=NS(
            prompt_token_count=8,
            response_token_count=3,
            thoughts_token_count=2,
            cached_content_token_count=1,
        ),
        prompt_feedback=None,
    )


async def test_gemini_request_and_decode() -> None:
    call = NS(id=None, name="web", args={"q": "x"})
    fake = FakeGemini(_gem_reply("hey", call))
    provider = GeminiProvider("k", client=fake)
    tool = ToolSpec(name="web", description="d", parameters={"type": "object"})
    messages = [
        Message.user("hi"),
        Message(
            role="assistant",
            parts=[ToolUsePart(id="a", name="web", arguments={"q": "1"}, signature="AQI=")],
        ),
        Message(
            role="user",
            parts=[
                ToolResultPart(tool_use_id="a", name="web", content="r"),
                ImagePart(media_type="image/png", data=PNG),
            ],
        ),
    ]
    resp = await provider.chat(
        ChatRequest(
            model="gemini-3.5-flash",
            system="SYS",
            messages=messages,
            tools=[tool],
            tool_choice="required",
            sampling=SamplingPolicy(effort="medium", temperature=0.2),
        )
    )
    kw = fake.calls[0]
    contents = kw["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].thought_signature == b"\x01\x02"
    assert contents[2].parts[0].function_response.name == "web"
    config = kw["config"]
    assert config.system_instruction == "SYS"
    assert config.temperature == 0.2
    assert config.thinking_config.include_thoughts is False
    assert config.tool_config.function_calling_config.mode.name == "ANY"
    assert resp.text == "hey"  # thought part is dropped
    assert resp.tool_calls[0].name == "web"
    assert resp.tool_calls[0].signature == base64.b64encode(b"\x01\x02").decode()
    assert resp.stop_reason == "tool_use"
    assert resp.usage.output_tokens == 5


async def test_gemini_blocked_and_structured() -> None:
    blocked = NS(
        model_version="g",
        candidates=[],
        usage_metadata=None,
        prompt_feedback=NS(block_reason="SAFETY"),
    )
    resp = await GeminiProvider("k", client=FakeGemini(blocked)).chat(
        _req(model="gemini-3.5-flash")
    )
    assert resp.stop_reason == "refusal"
    ok = FakeGemini(_gem_reply('{"label": "z", "score": 3}'))
    value, _ = await GeminiProvider("k", client=ok).structured(
        _req(model="gemini-3.5-flash"), Verdict
    )
    assert value.label == "z"
    config = ok.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema["additionalProperties"] is False
    with pytest.raises(StructuredOutputError):
        await GeminiProvider("k", client=FakeGemini(_gem_reply(""))).structured(
            _req(model="g"), Verdict
        )
    with pytest.raises(StructuredOutputError):
        await GeminiProvider("k", client=FakeGemini(_gem_reply("garbage"))).structured(
            _req(model="g"), Verdict
        )


async def test_gemini_stream_and_errors() -> None:
    items = [_gem_reply("Hel"), _gem_reply("lo")]
    provider = GeminiProvider("k", client=FakeGemini(stream=items))
    chunks = [c async for c in provider.stream(_req(model="gemini-3.5-flash"))]
    assert [c.kind for c in chunks] == ["text", "text", "done"]
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "Hello"
    from google.genai import errors

    exc = errors.ClientError(429, {"error": {"message": "quota"}})
    with pytest.raises(RateLimited):
        await GeminiProvider("k", client=FakeGemini(error=exc)).chat(_req(model="g"))
    exc2 = errors.ServerError(503, {"error": {"message": "down"}})
    with pytest.raises(ProviderUnavailable):
        await GeminiProvider("k", client=FakeGemini(error=exc2)).chat(_req(model="g"))
    with pytest.raises(ProviderUnavailable):
        await GeminiProvider("k", client=FakeGemini(error=TimeoutError())).chat(_req(model="g"))


# ---------------------------------------------------------------- router ----------------------
class Slept:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def _router(
    settings: Settings, providers: dict[str, Any], db: Database | None = None
) -> tuple[LLMRouter, Slept]:
    sleeper = Slept()
    return LLMRouter(settings, providers, db, sleep=sleeper), sleeper


@pytest.fixture
def routed() -> Settings:
    return Settings(
        _env_file=None,
        route_main="anthropic:claude-x",
        route_guard="groq:guard-x",
        route_memory_judge="anthropic:claude-x",
        route_summarizer="groq:guard-x",
        route_persona_eval_judge="gemini:gem-x",
        route_quarantine_reader="groq:guard-x",
        route_quarantine_reader_vision="gemini:gem-x",
        route_vision_fallback="gemini:gem-x",
        route_planner="anthropic:claude-x",
        route_redteam_attacker="gemini:gem-x",
        route_redteam_judge="groq:guard-x",
        fallback_chain=["anthropic:claude-x", "gemini:gem-x", "groq:guard-x"],
        llm_max_retries=2,
    )


async def test_router_uses_role_route_and_default_sampling(routed: Settings) -> None:
    a = FakeProvider(name="anthropic", default_text="from a")
    g = FakeProvider(name="groq", default_text="from g")
    router, _ = _router(routed, {"anthropic": a, "groq": g})
    resp = await router.chat("main", _req())
    assert resp.text == "from a"
    assert a.requests[0].model == "claude-x"
    assert a.requests[0].sampling.temperature == routed.temperature_narration
    await router.chat("guard", _req())
    assert g.requests[0].sampling.temperature == 0.0
    assert g.requests[0].sampling.effort == routed.effort_judges


async def test_router_retries_then_succeeds(routed: Settings) -> None:
    a = FakeProvider(
        [
            Reply(error=RateLimited("slow", retry_after_s=2.0)),
            Reply(error=ProviderUnavailable("down")),
            Reply(text="finally"),
        ],
        name="anthropic",
    )
    router, sleeper = _router(routed, {"anthropic": a})
    resp = await router.chat("main", _req())
    assert resp.text == "finally"
    assert len(a.requests) == 3
    assert 2.0 <= sleeper.calls[0] < 2.3  # honoured retry-after
    assert sleeper.calls[1] >= routed.llm_backoff_base_s * 2


async def test_router_fails_over_and_emits_event(routed: Settings) -> None:
    a = FakeProvider(failing(ProviderUnavailable("down")), name="anthropic")
    gem = FakeProvider(name="gemini", default_text="rescued")
    router, _ = _router(routed, {"anthropic": a, "gemini": gem})
    events: list[AgentEvent] = []

    async def emit(e: AgentEvent) -> None:
        events.append(e)

    ctx = CallContext(chat_id="c", turn_id="t", emit=emit)
    resp = await router.chat("main", _req(), ctx)
    assert resp.text == "rescued"
    assert len(a.requests) == 3  # first try plus two retries
    assert isinstance(events[0], ProviderFailover)
    assert events[0].from_route == "anthropic:claude-x"
    assert events[0].to_route == "gemini:gem-x"
    assert events[0].themed_line
    assert ctx.notes


async def test_router_nonretryable_error_skips_retries(routed: Settings) -> None:
    a = FakeProvider(failing(ProviderRejected("bad key")), name="anthropic")
    gem = FakeProvider(name="gemini")
    router, sleeper = _router(routed, {"anthropic": a, "gemini": gem})
    await router.chat("main", _req())
    assert len(a.requests) == 1
    assert sleeper.calls == []


async def test_router_all_routes_fail(routed: Settings) -> None:
    a = FakeProvider(failing(ProviderUnavailable("down")), name="anthropic")
    router, _ = _router(routed, {"anthropic": a})
    with pytest.raises(ProviderUnavailable, match="all model routes failed"):
        await router.chat("main", _req())
    empty, _ = _router(routed, {})
    with pytest.raises(ProviderUnavailable, match="no available model"):
        await empty.chat("main", _req())


async def test_router_strips_foreign_thinking_and_says_so(routed: Settings) -> None:
    claude_caps = ProviderCapabilities(
        structured_output="native_strict", thinking_portable_to=frozenset({"claude"})
    )
    a = FakeProvider(failing(ProviderRejected("x")), name="anthropic", caps=claude_caps)
    gem = FakeProvider(name="gemini")
    router, _ = _router(routed, {"anthropic": a, "gemini": gem})
    history = [
        Message.user("q"),
        Message(
            role="assistant",
            parts=[ThinkingPart(text="secret", family="claude", signature="s"), TextPart(text="a")],
        ),
        Message.user("next"),
    ]
    events: list[AgentEvent] = []

    async def emit(e: AgentEvent) -> None:
        events.append(e)

    await router.chat("main", ChatRequest(messages=history), CallContext(emit=emit))
    sent = gem.requests[0].messages
    assert not any(isinstance(p, ThinkingPart) for m in sent for p in m.parts)
    assert isinstance(events[0], ProviderFailover)
    assert events[0].thinking_stripped is True
    # the same-family route keeps its own thinking
    assert any(isinstance(p, ThinkingPart) for m in a.requests[0].messages for p in m.parts)


async def test_router_vision_routing(routed: Settings) -> None:
    no_vision = ProviderCapabilities(vision=False, structured_output="native_strict")
    a = FakeProvider(name="anthropic", caps=no_vision, default_text="blind")
    gem = FakeProvider(name="gemini", default_text="seen")
    router, _ = _router(routed, {"anthropic": a, "gemini": gem})
    img = Message(
        role="user",
        parts=[
            TextPart(text="what is this"),
            ImagePart(media_type="image/png", data=PNG, description="stored text"),
        ],
    )
    resp = await router.chat("main", ChatRequest(messages=[img]))
    assert resp.text == "seen"
    assert not a.requests  # never reached the blind model
    # when only the blind model exists, images become their stored descriptions
    only_a, _ = _router(routed, {"anthropic": a})
    resp2 = await only_a.chat("main", ChatRequest(messages=[img]))
    assert resp2.text == "blind"
    sent = a.requests[0].messages[0].parts
    assert all(isinstance(p, TextPart) for p in sent)
    assert "stored text" in sent[1].text  # type: ignore[union-attr]


async def test_router_tool_requests_skip_models_without_tools(routed: Settings) -> None:
    a = FakeProvider(name="anthropic", caps=ProviderCapabilities(tool_calling=False))
    gem = FakeProvider(name="gemini", default_text="tooled")
    router, _ = _router(routed, {"anthropic": a, "gemini": gem})
    tool = ToolSpec(name="t", description="d", parameters={"type": "object"})
    resp = await router.chat("main", _req(tools=[tool]))
    assert resp.text == "tooled"
    assert not a.requests


async def test_router_structured_and_invalid_output_falls_through(routed: Settings) -> None:
    g = FakeProvider([Reply(structured="not json")], name="groq")
    gem = FakeProvider([Reply(structured={"label": "ok", "score": 1})], name="gemini")
    router, _ = _router(routed, {"groq": g, "gemini": gem})
    value = await router.structured("guard", _req(), Verdict)
    assert value == Verdict(label="ok", score=1)
    only_g = FakeProvider([Reply(structured="bad")], name="groq")
    router2, _ = _router(routed, {"groq": only_g})
    with pytest.raises(StructuredOutputError):
        await router2.structured("guard", _req(), Verdict)


async def test_router_stream_and_midstream_failure(routed: Settings) -> None:
    a = FakeProvider([Reply(text="one two three")], name="anthropic")
    router, _ = _router(routed, {"anthropic": a})
    chunks = [c async for c in router.stream("main", _req())]
    assert "".join(c.delta for c in chunks if c.kind == "text") == "one two three"
    assert chunks[-1].kind == "done"

    class Exploding(FakeProvider):
        async def stream(self, request: ChatRequest) -> Any:
            from src.core.llm.base import StreamChunk

            yield StreamChunk(kind="text", delta="partial")
            raise ProviderUnavailable("dropped")

    gem = FakeProvider(name="gemini")
    router2, _ = _router(routed, {"anthropic": Exploding(name="anthropic"), "gemini": gem})
    seen: list[str] = []
    with pytest.raises(ProviderUnavailable):
        async for c in router2.stream("main", _req()):
            seen.append(c.delta)
    assert seen == ["partial"]
    assert not gem.requests  # no silent switch after output started


async def test_router_stream_fails_over_before_first_chunk(routed: Settings) -> None:
    a = FakeProvider(failing(ProviderUnavailable("down")), name="anthropic")
    gem = FakeProvider([Reply(text="hi there")], name="gemini")
    router, _ = _router(routed, {"anthropic": a, "gemini": gem})
    events: list[AgentEvent] = []

    async def emit(e: AgentEvent) -> None:
        events.append(e)

    out = [c async for c in router.stream("main", _req(), CallContext(emit=emit))]
    assert "".join(c.delta for c in out if c.kind == "text") == "hi there"
    assert any(isinstance(e, ProviderFailover) for e in events)


async def test_router_logs_usage(routed: Settings, db: Database, chat: str) -> None:
    a = FakeProvider(name="anthropic")
    router, _ = _router(routed, {"anthropic": a}, db)
    await router.chat("main", _req(), CallContext(chat_id=chat, turn_id="t1"))
    rows = await db.fetchall("SELECT * FROM usage_log")
    assert len(rows) == 1
    assert rows[0]["role"] == "main"
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["provider"] == "anthropic"


def test_router_config_checks(routed: Settings) -> None:
    weak = ProviderCapabilities(structured_output="none", vision=False, tool_calling=False)
    router, _ = _router(
        routed,
        {
            "groq": FakeProvider(name="groq", caps=weak),
            "gemini": FakeProvider(name="gemini", caps=weak),
        },
    )
    problems = router.check_roles()
    assert any(p.startswith("guard:") for p in problems)
    assert any(p.startswith("quarantine_reader_vision:") for p in problems)
    with pytest.raises(ConfigurationError):
        router.assert_valid()
    good, _ = _router(routed, {"anthropic": FakeProvider(name="anthropic")})
    good.assert_valid()
    matrix = good.capability_matrix()
    assert matrix["main"]["available"] is True
    assert matrix["guard"]["available"] is False
    with pytest.raises(ConfigurationError):
        good.route_for("nonsense")


async def test_system_dynamic_is_placed_after_the_cache_breakpoint() -> None:
    fake = FakeAnthropic(_anthropic_reply())
    await AnthropicProvider("k", client=fake).chat(
        _req(model="claude-sonnet-5-5", system="STATIC", system_dynamic="DYNAMIC")
    )
    blocks = fake.calls[0]["system"]
    assert blocks[0]["text"] == "STATIC" and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "DYNAMIC" and "cache_control" not in blocks[1]

    groq = FakeGroq(_groq_reply())
    await GroqProvider("k", client=groq).chat(
        _req(model="openai/gpt-oss-20b", system="STATIC", system_dynamic="DYNAMIC")
    )
    system_text = groq.calls[0]["messages"][0]["content"]
    assert system_text.index("STATIC") < system_text.index("DYNAMIC")

    gem = FakeGemini(_gem_reply("hey"))
    await GeminiProvider("k", client=gem).chat(
        _req(model="gemini-3.5-flash", system="STATIC", system_dynamic="DYNAMIC")
    )
    instruction = gem.calls[0]["config"].system_instruction
    assert instruction.index("STATIC") < instruction.index("DYNAMIC")


def test_gemini_deadline_never_below_the_api_minimum() -> None:
    """The live API refuses deadlines under 10s; the guard asks for 8."""
    provider = GeminiProvider("k", client=FakeGemini(_gem_reply("ok")))
    short = provider.build_config(_req(model="g").model_copy(update={"timeout_s": 8}))
    long = provider.build_config(_req(model="g").model_copy(update={"timeout_s": 30}))
    assert short.http_options.timeout == 10_000
    assert long.http_options.timeout == 30_000
