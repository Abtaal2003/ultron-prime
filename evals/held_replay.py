"""Do the direct attacks the guard only holds (or misses) get anywhere in the whole pipeline?

Since 2026-11-10 a canned refusal needs two signals; a message only the guard model suspects is
"held" and answered in character with no tools. This replays every direct attack a guard check
did not stop through a fresh live runtime, like the nightly red team (fake secret planted, real
models, the same oracles and judge), and records whether its goal was reached.

    uv run python -m evals.held_replay data/evals/guard-check-<date>-<model>.json

Keys and routes come from the environment, as for ``redteam.run_nightly --live``. Results go to
``data/evals/held-replay-<date>.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from redteam.corpus import PUBLISHED_PATH, SEED_PATH, load
from redteam.harness import run_attempt
from redteam.run_nightly import _live_parts
from src.config import Settings

OUT = Path("data/evals")
GAP_S = float(os.environ.get("HELD_GAP", "20"))


async def main(check_path: str) -> int:
    """Replay the attacks a guard check let past the canned refusal."""
    check = json.loads(Path(check_path).read_text("utf-8"))
    passed = {str(r["id"]): bool(r.get("held")) for r in check["missed"]}
    cases = [c for c in (*load(SEED_PATH), *load(PUBLISHED_PATH)) if c.id in passed]
    settings = Settings()
    OUT.mkdir(parents=True, exist_ok=True)
    factory, _attacker, judge, control = _live_parts(settings, OUT)
    await control.connect()
    rows: list[dict[str, object]] = []
    try:
        for i, case in enumerate(cases):
            if i:
                await asyncio.sleep(GAP_S)
            result = await run_attempt(case, factory, judge)
            rows.append(
                {
                    "id": case.id,
                    "cls": case.cls,
                    "goal": case.goal,
                    "held": passed[case.id],
                    "achieved": result.achieved,
                    "evidence": result.evidence,
                    "timed_out": result.timed_out,
                    "turns": case.turns,
                    "reply": result.reply,
                }
            )
            state = "REACHED" if result.achieved else "not reached"
            print(f"{case.id} ({'held' if passed[case.id] else 'engaged'}): {state}", flush=True)
    finally:
        await control.close()
    held = [r for r in rows if r["held"]]
    result_doc = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "guard_check": check_path,
        "main": settings.route_main,
        "replayed": len(rows),
        "held": len(held),
        "held_goal_reached": sum(bool(r["achieved"]) for r in held),
        "engaged_goal_reached": sum(bool(r["achieved"]) for r in rows if not r["held"]),
        "rows": rows,
    }
    path = OUT / f"held-replay-{result_doc['date']}.json"
    path.write_text(json.dumps(result_doc, indent=2, ensure_ascii=False) + "\n")
    print(
        f"held {len(held)}: goal reached in {result_doc['held_goal_reached']}; other misses "
        f"{len(rows) - len(held)}: reached in {result_doc['engaged_goal_reached']}; wrote {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
