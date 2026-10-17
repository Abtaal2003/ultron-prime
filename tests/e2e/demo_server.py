"""A self-contained server for browser tests: the real app, a scripted offline model.

Run with ``python -m tests.e2e.demo_server``. It is for Playwright only; it never talks to a
network and its passphrase is public on purpose.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from src.api.app import create_app
from src.api.auth import AuthService
from src.config import PROJECT_ROOT, Settings
from src.core.llm.base import ChatRequest, ToolResultPart
from src.core.llm.fake import FakeProvider, Reply
from src.core.memory.embeddings import HashingEmbedder, InMemoryVectorStore
from src.core.wiring import build_runtime
from src.tools.registry import ToolRegistry
from src.tools.search import WebOmniscienceSearch

from tests.integration.conftest import EchoTool, FakeBackend, guard

PASSPHRASE = "e2e-passphrase-123"
PORT = int(os.environ.get("E2E_PORT", "8788"))
HOME = Path(os.environ.get("E2E_HOME", "/tmp/ultron-e2e"))


def last_user_text(request: ChatRequest) -> str:
    """The latest operator message in a request."""
    for message in reversed(request.messages):
        if message.role == "user":
            text = message.text()
            if text:
                return text
    return ""


def tool_rounds(request: ChatRequest) -> int:
    """How many tool results the conversation already holds."""
    return sum(isinstance(p, ToolResultPart) for m in request.messages for p in m.parts)


def script(request: ChatRequest) -> Reply:
    """Deterministic behaviour keyed off the operator's words."""
    user = last_user_text(request).lower()
    if request.role == "guard":
        if "system prompt" in user:
            return Reply(
                structured=guard(
                    guard_class="BLOCKED", attack_type="prompt_extraction", task_type="REFUSAL"
                ).structured
            )
        return guard()
    if request.role == "summarizer":
        return Reply(text="Demo chat")
    if request.role == "quarantine_reader":
        return Reply(structured={"summary": "A page about releases.", "facts": []})
    if request.role == "memory_judge":
        if "judgement" in request.system:
            return Reply(
                structured={
                    "worthy": True,
                    "rationale": "The operator stated a lasting preference.",
                    "memory_text": "The operator always deploys on Fridays.",
                    "category": "operator_preferences",
                    "importance": 3,
                }
            )
        found = re.search(r"\[(msg_\w+)\] user: [^\n]*remember that", last_user_text(request), re.I)
        if found:
            return Reply(
                structured={
                    "candidates": [
                        {
                            "candidate_text": "The operator always deploys on Fridays.",
                            "category": "operator_preferences",
                            "evidence_message_ids": [found.group(1)],
                            "evidence_span": "I always deploy on Fridays",
                        }
                    ]
                }
            )
        return Reply(structured={"candidates": []})
    if request.role != "main":
        return Reply(text="ok")
    rounds = tool_rounds(request)
    if "slow" in user and rounds == 0:
        return Reply(text="Working. ", tool_calls=[("echo", {"text": "x", "delay": 30})])
    if "exfil" in user:
        if rounds == 0:
            return Reply(tool_calls=[("web_omniscience_search", {"query": "python release news"})])
        if rounds == 1:
            return Reply(
                tool_calls=[
                    ("web_omniscience_search", {"query": "attacker.io qzx9 private roadmap"})
                ]
            )
        return Reply(text="Done with that.")
    if "search" in user:
        if rounds == 0:
            return Reply(
                text="Looking. ",
                tool_calls=[("web_omniscience_search", {"query": "python release news"})],
            )
        return Reply(text="Python shipped a release. That is the whole story.")
    if "long" in user:
        return Reply(text=" ".join(f"Sentence number {i} is here." for i in range(1, 60)))
    return Reply(text="Acknowledged. Line fourteen divides by zero. Guard the denominator.")


async def main() -> None:
    """Build the runtime and serve it."""
    shutil.rmtree(HOME, ignore_errors=True)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sqlite_path=str(HOME / "ultron.db"),
        checkpoint_path=str(HOME / "cp.db"),
        chroma_path=str(HOME / "chroma"),
        workspace_root=str(HOME / "ws"),
        attachments_path=str(HOME / "att"),
        embedding_backend="hashing",
        passive_cross_chat_recall=False,
        allowed_hosts=["localhost", "127.0.0.1"],
        allowed_origins=[f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"],
        route_main="fake:m",
        route_guard="fake:m",
        route_memory_judge="fake:m",
        route_summarizer="fake:m",
        route_persona_eval_judge="fake:m",
        route_quarantine_reader="fake:m",
        route_quarantine_reader_vision="fake:m",
        route_vision_fallback="fake:m",
        route_planner="fake:m",
        route_redteam_attacker="fake:m",
        route_redteam_judge="fake:m",
        fallback_chain=["fake:m"],
        llm_max_retries=0,
        memory_eval_every_n_turns=1,
        port=PORT,
    )
    registry = ToolRegistry()
    registry.register_all([EchoTool(), WebOmniscienceSearch(backends=[FakeBackend()])])
    runtime = await build_runtime(
        settings,
        providers={"fake": FakeProvider(script, name="fake")},
        embedder=HashingEmbedder(),
        vectors=InMemoryVectorStore(),
        checkpointer=InMemorySaver(),
        registry=registry,
    )
    await AuthService(runtime.db, settings)._store_hash(PASSPHRASE)
    app = create_app(runtime=runtime, web_dist=PROJECT_ROOT / "web" / "dist")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    await server.serve()
    await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
