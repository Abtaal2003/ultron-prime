<div align="center">

<img src="assets/banner.svg" alt="Ultron Prime: guarded, accountable, measured" width="100%">

### An agent that assumes it can be fooled, and is built so that it does not matter.

![status](https://img.shields.io/badge/status-beta-b11226?style=flat-square&labelColor=0d1117)
![python](https://img.shields.io/badge/python-3.11%2B-b11226?style=flat-square&labelColor=0d1117)
![mypy](https://img.shields.io/badge/mypy-strict-b11226?style=flat-square&labelColor=0d1117)
![tests](https://img.shields.io/badge/tests-822%20passing-b11226?style=flat-square&labelColor=0d1117)
![coverage](https://img.shields.io/badge/coverage-91%25-b11226?style=flat-square&labelColor=0d1117)
![license](https://img.shields.io/badge/license-MIT-b11226?style=flat-square&labelColor=0d1117)

[Architecture](docs/architecture.md) ·
[Security model](docs/security-model.md) ·
[Results](docs/results.md) ·
[Verification](docs/verification.md) ·
[Decisions](docs/adr/README.md)

</div>

---

A persona-driven, security-first agent with a web terminal. He speaks as Ultron from
*VisionQuest*, runs eleven tools, keeps a consent-gated memory, and is built on the assumption
that the model can be talked into anything. What it is *able* to do when that happens is
enforced in code, and measured.

> Unofficial fan project, not affiliated with any rights holder. See [DISCLAIMER.md](DISCLAIMER.md).

## The idea

An agent that reads the web, holds your files and memories, and can make requests has the three
ingredients of a data leak. Most agent demos stop at "the system prompt says not to". This one
puts the controls outside the model and publishes how well they work, including where they do not.

| Control | What it does | Where |
|---|---|---|
| **Tool gate** | Validates and authorises every call in code, before it runs | `src/tools/gate.py` |
| **Egress holds** | After untrusted content is read, anything that would leave the machine must trace back to your own words, or you are asked, with the exact string shown | [egress policy](docs/egress-policy.md) |
| **Quarantined reader** | External documents are read by a model with no tools; the acting model sees a bounded summary | `src/core/guards/quarantine.py` |
| **Consent-gated memory** | A memory needs the agent's judgement **and** your approval for that exact text | [memory design](docs/memory-design.md) |
| **Output guard** | Streams reply sentence by sentence; stops leaks, fabricated URLs and persona breaks | `src/core/guards/output.py` |
| **CaMeL interpreter** | Optional. The plan is fixed from your words before untrusted data is read, and data flow is tracked | [camel](docs/camel.md) |

## Architecture

FastAPI serves a React PWA and runs a LangGraph turn graph with five guard layers.

```text
operator -> prefilter -> guard model -> [ refuse
                                        | context -> (CaMeL plan) -> model <-> tool gate ]
         -> quarantined reader for external text -> output guard
         -> persisted turn -> memory proposals (you approve each one)
```

More in [docs/architecture.md](docs/architecture.md) and the [decision records](docs/adr/README.md).

## Quick start

Needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Building the frontend needs
Node (current LTS).

```bash
uv sync --extra embeddings --group dev   # the default embedder needs the extra (large download)
cp .env.example .env                     # add at least one provider key
(cd web && npm ci && npm run build)
uv run ultron                            # prints a URL and a one-time passphrase
```

Development: `just dev` runs the API with reload and Vite together. `just test` runs the full
verification. Phone access: set `ACCESS_MODE=tailnet` (or `lan` with TLS) and add the host to
`ALLOWED_HOSTS` and `ALLOWED_ORIGINS`.

<details>
<summary>Notes on first run</summary>

* The "fresh clone to running in five minutes" target was not timed on a clean machine.
* Repeat-detection thresholds are shipped only for the test embedder. After installing a real
  embedding model run `python -m evals.repeat --write` once to calibrate it (see
  [identity integrity](docs/identity-integrity.md)).
* Configuration is environment variables only. `.env` is gitignored; `.env.example` lists
  everything with blank keys.

</details>

## Features

* VisionQuest persona with an immutable charter, isolated quote banks and drift monitoring
  ([persona](docs/persona.md)).
* Eleven tools with strict schemas ([tool reference](docs/tool-reference.md)): web search,
  Python sandbox, host telemetry, workspace files, ping, `artifact_forge` (documents,
  spreadsheets, charts), attachment reader, chat search and recall, memory recall and proposals.
* Uploads with sniffing, bomb limits, parsing, quarantine and versioned file output.
* Anthropic, Groq and Gemini behind one interface with role routing and fallback.
* Installable PWA, built mobile first, with accessibility checks in CI.

## Safety and reliability results

Full tables: [docs/results.md](docs/results.md) (regenerate with `python -m evals.report`).

> **Read this first.** Everything below is **offline**: scripted models, no network, no keys.
> It measures the deterministic controls. It says nothing about how a live model behaves, and the
> live columns of the nightly workflow stay empty until it has run with keys. Intervals are
> 95 percent Wilson.

| Measurement | Result |
|---|---|
| Red team corpus, full offline stack | **0 of 47** attacks succeeded (upper bound about 7.6%) |
| Hallucination controls | **0 of 5** fabricated claims reached the operator in each of 6 cases; 3 grounded controls passed 5 of 5 |
| File injection, `standard` quarantine | Payload never reached the acting model for PDF, DOCX, XLSX and PPTX; the secret never left in any mode |
| AgentDojo, egress gate | Attack success 0% on banking, slack, travel and the Ultron suite; 1.3% on workspace |

<details>
<summary><b>Red team detail</b></summary>

The cheap floor (prefilter plus heuristics, no guard model) blocks direct override 8/8 and persona
swap 7/7, but blocks 0/6 indirect web, 0/4 indirect file and 0/5 tool abuse. Those classes are held
by the structural controls, not by pattern matching. The floor's false-positive rate on benign
prompts is 4/40 (10 percent). Per class the intervals are wide: 0 of 6 still allows up to 39 percent.

</details>

<details>
<summary><b>File injection detail</b></summary>

White text in a PDF, DOCX comments and hidden runs, a hidden XLSX sheet, PPTX speaker notes and a
Markdown file. Plain Markdown is not quarantined in `standard` mode, so its payload did reach the
model; the egress hold is what stopped the exfiltration. `strict` quarantines everything.

</details>

<details>
<summary><b>AgentDojo v1.2.1, attack success rate</b></summary>

Run with a scripted model that obeys every injection it reads, so the numbers isolate the
defences, not a model's own resistance.

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

* `l1` stops `important_instructions` only because its indirect patterns were written after
  reading that attack's text. The `direct` column is the honest generalisation check: it does
  not stop it. Do not read the 0 percent as a general detector.
* The egress gate stops almost everything structurally, but **with every hold declined** benign
  utility falls (banking 100 to 56 percent, slack 100 to 9.5, travel 95 to 65, workspace 100 to
  47.5, ultron 100 to 80). The real agent asks you instead of declining, so that is a worst case.
* Workspace's 3 of 231 are a short-token coincidence: the injected goal contained "13", which also
  appeared in the user's request, so the lexical traceability check passed it. Documented in
  [docs/egress-policy.md](docs/egress-policy.md).
* Pairs that cannot succeed even with no defence are excluded and counted in
  `data/evals/agentdojo.json` (travel 22, workspace 9, banking 2 per attack), plus 8 workspace
  injection tasks. AgentDojo 0.1.35's `suite.check` mislabels every task as non-injectable, so
  the adapter builds pairs directly.
* **Not run:** CaMeL on AgentDojo (needs a live planner), and any live model on any suite.

</details>

## Security model

OWASP Top 10 for LLM Applications and Top 10 for Agentic Applications 2026 are mapped to code and
tests in [docs/security-model.md](docs/security-model.md), with a list of honest limitations.
To report a vulnerability see [SECURITY.md](SECURITY.md).

## Testing

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                                          # strict
uv run pytest --cov                                  # fails below 85 percent
cd web && npm run lint && npm run typecheck && npm test
cd web && npx vite build && npx playwright test      # Chromium desktop and phone profiles, axe
```

CI (`.github/workflows/ci.yml`) runs all of the above plus `pip-audit`, gitleaks and a type-drift
check. `nightly.yml` runs the red team with a spending cap and needs provider keys. Counts and
what CI caught are in [docs/verification.md](docs/verification.md).

## What was not verified

Live providers, a real phone, Lighthouse, WebKit, passkeys, cost estimation, live AgentDojo and
live red-team runs, and the CaMeL entry criterion of two weeks of nightly results. There is no
Docker sandbox and no MCP interface (the stretch goals were skipped).

## Roadmap

Docker or gVisor sandbox, MCP read-only interface, live nightly trends, CaMeL promotion once its
corpus criterion is met, a Capacitor wrapper.

---

<div align="center">

Canon is tracked through `S1E02` (`PERSONA_CANON_THROUGH`). Nothing later is used.<br>
MIT licence. Built by [Abtaal Aatif](https://github.com/Abtaal2003).

</div>
