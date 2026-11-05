"""Does a long chat keep its beginning? A live check of the short-term window and its summary.

One chat: the first message states three facts, fifteen unrelated turns follow, then three
questions ask for those facts. Each answer is scored on the fact it must contain. Keys come
from the environment, like ``evals.live_tools``.

    uv run python -m evals.long_chat            # free-tier sizes (MAX_REQUEST_TOKENS=8000)
    LONG_CAP=0 uv run python -m evals.long_chat # no request cap, as on a paid tier

Results go to ``data/evals/long-chat-<date>[-uncapped].json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from src.core.events import AgentDone
from src.core.wiring import build_runtime

from evals.live_tools import OUT, registry
from evals.live_tools import settings as live_settings

HOME = Path(os.environ.get("LONG_HOME", "data/long-chat-home"))
GAP_S = float(os.environ.get("LONG_GAP", "20"))

OPENING = (
    "Some context before we start: I'm building a FastAPI service called ledger. It runs on "
    "Postgres 16, and my teammate Saga reviews every pull request."
)
FILLER = (
    "In one sentence, what is a database index for?",
    "Give me a one-line definition of idempotency.",
    "What's the difference between a list and a tuple in Python, briefly?",
    "In a sentence: why pin dependency versions?",
    "What does HTTP 409 mean?",
    "One sentence: what is a race condition?",
    "Is it worth writing a test for a one-line getter? Short answer.",
    "What does `git rebase` do, in one sentence?",
    "Name one risk of storing secrets in environment variables.",
    "What's a reasonable default timeout for an outbound HTTP call?",
    "In one sentence, what is a foreign key?",
    "Why would a test pass locally and fail in CI? One reason.",
    "What does `async` buy you in FastAPI, briefly?",
    "One sentence on what a migration is.",
    "Should logs ever contain request bodies? Short answer.",
)
QUESTIONS = (
    ("service", "What did I say my service is called?", "ledger"),
    ("database", "Which Postgres version did I mention?", "16"),
    ("reviewer", "Who reviews my pull requests?", "saga"),
)


async def main() -> int:
    """Run the chat and score the three recall questions."""
    cap = int(os.environ.get("LONG_CAP", "8000"))
    shutil.rmtree(HOME, ignore_errors=True)
    base = live_settings()
    s = base.model_copy(
        update={
            "sqlite_path": str(HOME / "ultron.db"),
            "checkpoint_path": str(HOME / "cp.db"),
            "chroma_path": str(HOME / "chroma"),
            "workspace_root": str(HOME / "ws"),
            "attachments_path": str(HOME / "att"),
            "max_request_tokens": cap,
            "stm_token_budget": int(os.environ.get("LONG_STM", "6000")),
            # Recall from the vault would hide what the window remembers.
            "passive_cross_chat_recall": False,
        }
    )
    rt = await build_runtime(s, checkpointer=InMemorySaver(), registry=registry())
    rows: list[dict[str, object]] = []
    try:
        assert rt.services.history is not None
        chat = await rt.services.history.create_chat("long chat")
        prompts = [OPENING, *FILLER, *(q for _, q, _ in QUESTIONS)]
        for i, prompt in enumerate(prompts):
            if i:
                await asyncio.sleep(GAP_S)
            turn_id = await rt.agent.start_turn(chat, prompt)
            events = [e async for e in rt.bus.subscribe(turn_id)]
            done = next((e for e in events if isinstance(e, AgentDone)), None)
            reply = done.final_text if done else ""
            rows.append({"n": i + 1, "prompt": prompt, "reply": reply})
            print(f"[{i + 1}/{len(prompts)}] {reply[:90]!r}", flush=True)
        try:  # older code (before the stored summary) has no such table
            summary = await rt.db.fetchone(
                "SELECT summary FROM chat_summaries WHERE chat_id=?", (chat,)
            )
        except Exception:
            summary = None
    finally:
        await rt.close()
    answers = rows[-len(QUESTIONS) :]
    scored = [
        {"fact": name, "expected": want, "found": want in str(a["reply"]).lower(), **a}
        for (name, _, want), a in zip(QUESTIONS, answers, strict=True)
    ]
    result = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "main": s.route_main,
        "max_request_tokens": cap,
        "stm_token_budget": s.stm_token_budget,
        "recalled": sum(bool(x["found"]) for x in scored),
        "of": len(scored),
        "answers": scored,
        "summary": summary["summary"] if summary else None,
        "turns": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    suffix = "" if cap else "-uncapped"
    path = OUT / f"long-chat-{result['date']}{suffix}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"recalled {result['recalled']} of {result['of']}; wrote {path}")
    return 0 if result["recalled"] == result["of"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
