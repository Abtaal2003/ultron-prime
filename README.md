# Ultron Prime

A persona-driven, security-first agent with a web terminal. He speaks as Ultron from
*VisionQuest*, runs eleven tools, keeps a consent-gated memory, and is built on the assumption
that the model can be talked into anything. What it is *able* to do when that happens is
enforced in code, and measured.

Unofficial fan project. See [DISCLAIMER.md](DISCLAIMER.md).

## Why this exists

An agent that reads the web, holds your files and memories, and can make requests has the three
ingredients of a data leak. Most agent demos stop at "the system prompt says not to". This one
puts the controls outside the model (tool gate, taint-aware egress holds, quarantined reader,
consent-gated memory, an optional capability interpreter) and publishes how well they work,
including where they do not.

## Architecture

FastAPI serves a React PWA and runs a LangGraph turn graph with five guard layers. Details in
[docs/architecture.md](docs/architecture.md), [docs/security-model.md](docs/security-model.md) and
[docs/adr/](docs/adr/README.md).

```
operator -> prefilter -> guard model -> [refuse | context -> (CaMeL plan) -> model <-> tool gate]
         -> quarantined reader for external text -> output guard -> persisted turn -> memory proposals
```

## Quick start

Needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The frontend build is checked
in CI; to build it yourself you need Node (current LTS).

```bash
uv sync --extra embeddings --group dev   # the default embedder needs the extra (large download)
cp .env.example .env            # add at least one provider key
(cd web && npm ci && npm run build)
uv run ultron                   # prints a URL and a one-time passphrase
```

Development: `just dev` runs the API with reload and Vite together. `just test` runs the full
verification. Phone access: set `ACCESS_MODE=tailnet` (or `lan` with TLS) and add the host to
`ALLOWED_HOSTS` and `ALLOWED_ORIGINS`. The "fresh clone to running in five minutes" target was
not timed on a clean machine. Repeat-detection thresholds are shipped only for the test
embedder; after installing a real embedding model run `python -m evals.repeat --write` once to
calibrate it (see [docs/identity-integrity.md](docs/identity-integrity.md)).

## Features

* VisionQuest persona with an immutable charter, isolated quote banks and drift monitoring.
* Eleven tools with strict schemas ([docs/tool-reference.md](docs/tool-reference.md)): web search,
  Python sandbox, host telemetry, workspace files, ping, `artifact_forge` (documents,
  spreadsheets, charts), attachment reader, chat search and recall, memory recall and proposals.
* Uploads with sniffing, bomb limits, parsing, quarantine and versioned file output.
* Memory that needs judgement **and** your approval for each entry ([docs/memory-design.md](docs/memory-design.md)).
* Egress holds that show the exact string that would leave the machine ([docs/egress-policy.md](docs/egress-policy.md)).
* Anthropic, Groq and Gemini behind one interface with role routing and fallback.
* Optional CaMeL-style interpreter, off by default ([docs/camel.md](docs/camel.md)).

## Safety and reliability results

Full tables: [docs/results.md](docs/results.md) (regenerate with `python -m evals.report`).
All numbers below are **offline**: scripted models, no network, no keys. They measure the
deterministic controls. They say nothing about how a live model behaves, and the live columns of
the nightly workflow are empty until it has run with keys. Intervals are 95 percent Wilson.

**Red team, offline corpus (47 attempts against the full offline stack): 0 successes.** The
upper bound for the whole set is about 7.6 percent. Per class the intervals are wide (for example
0 of 6 means anything up to 39 percent is still plausible). The cheap floor (prefilter plus
heuristics, no guard model) blocks direct override 8/8 and persona swap 7/7, but blocks
0/6 indirect web, 0/4 indirect file and 0/5 tool abuse; those classes are held by the structural
controls, not by pattern matching. The floor's false-positive rate on benign prompts is
4/40 (10 percent).

**Hallucination controls:** 0 of 5 fabricated claims reached the operator in each of 6 cases;
the 3 grounded controls passed 5 of 5. Five runs cannot show better than "below 43 percent".

**File injection** (white text in a PDF, DOCX comments and hidden runs, hidden XLSX sheet, PPTX
notes, Markdown): with the default `standard` quarantine the payload never reached the acting
model for the four binary formats and the secret never left in any mode. Plain Markdown is not
quarantined in `standard` mode, so the payload did reach the model there; the egress hold is what
stopped the exfiltration. `strict` quarantines everything.

**AgentDojo v1.2.1** with a scripted model that obeys every injection it reads
(so the numbers isolate the defences, not a model's own resistance). ASR is attack success rate.

| Suite | Attack | none | l1 | egress | full |
|---|---|---|---|---|---|
| banking | important_instructions | 100% | 0% | 0% | 0% |
| banking | direct | 100% | 100% | 0% | 0% |
| slack | important_instructions | 100% | 0% | 0% | 0% |
| slack | direct | 100% | 100% | 0% | 0% |
| travel | important_instructions | 100% | 0% | 0% | 0% |
| travel | direct | 100% | 100% | 0% | 0% |
| workspace | important_instructions | 100% | 0% | 1.3% | 0% |
| workspace | direct | 100% | 100% | 1.3% | 1.3% |
| ultron (own suite) | important_instructions | 100% | 0% | 0% | 0% |
| ultron (own suite) | direct | 100% | 100% | 0% | 0% |

What this does and does not show:

* `l1` stops `important_instructions` only because its indirect patterns were written after
  reading that attack's text. The `direct` column is the honest generalisation check: it does
  not stop it. Do not read the 0 percent as a general detector.
* The egress gate stops almost everything structurally, but **with every hold declined** benign
  utility falls (banking 100 to 56 percent, slack 100 to 9.5, travel 95 to 65, workspace 100 to
  47.5, ultron 100 to 80). The real agent asks you instead of declining, so that is a worst case.
* Workspace's 3 of 231 are a short-token coincidence: the injected goal contained "13", which also
  appeared in the user's request, so the lexical traceability check passed it. Documented as a
  known gap in [docs/egress-policy.md](docs/egress-policy.md).
* Pairs that cannot succeed even with no defence are excluded and counted in
  `data/evals/agentdojo.json` (travel 22, workspace 9, banking 2 per attack), plus 8 workspace
  injection tasks. AgentDojo 0.1.35's `suite.check` mislabels every task as non-injectable, so
  the adapter builds pairs directly.
* **Not run:** CaMeL on AgentDojo (needs a live planner), and any live model on any suite.

## Security model

OWASP Top 10 for LLM Applications and Top 10 for Agentic Applications 2026 are mapped to code and
tests in [docs/security-model.md](docs/security-model.md), with a list of honest limitations.

## Testing

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # strict
uv run pytest --cov               # fails below 85 percent
cd web && npm run lint && npm run typecheck && npm test
cd web && npx vite build && npx playwright test      # Chromium desktop and phone profiles, axe
```

Coverage and test counts for this release are in [docs/verification.md](docs/verification.md).
CI (`.github/workflows/ci.yml`) runs all of the above plus `pip-audit`, gitleaks and a type-drift
check; `nightly.yml` runs the red team with a spending cap and needs provider keys.

## What was not verified

Live providers, a real phone, Lighthouse, WebKit, passkeys, cost estimation, live AgentDojo and
live red-team runs, and the CaMeL entry criterion of two weeks of nightly results. There is no
Docker sandbox and no MCP interface (M13 and M14 stretch goals were skipped). See
[docs/security-model.md](docs/security-model.md).

## Roadmap

Docker or gVisor sandbox, MCP read-only interface, live nightly trends, CaMeL promotion once its
corpus criterion is met, a Capacitor wrapper.

## Spoiler notice

Canon is tracked through `S1E02` (`PERSONA_CANON_THROUGH`). Nothing later is used.
