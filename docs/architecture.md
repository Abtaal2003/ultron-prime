# Architecture

Ultron Prime is one Python process (FastAPI) that serves a React single page app and runs a guarded
agent. Everything the operator sees is an event on a bus; everything that matters for safety is
code that the model cannot talk its way past.

```
 React PWA (web/)  <-- SSE, resumable -->  FastAPI (src/api/)
                                              |
                                    AgentService / Agent (src/core/agent.py)
                                              |
        LangGraph StateGraph, one run per turn
        ingest_attachments -> guard_input -> detect_repeat -+-> refuse_turn ------------+
                                                             +-> retrieve_context       |
                                                                   |                    |
                                                              camel_run                 |
                                                                   |                    |
                                          +----------------> llm_decide <---------------+
                                          |                       | tool calls
                                          |               tool_gate -> egress_wait (interrupt)
                                          |                       |           |
                                          |               tool_executor <-----+
                                          |                       |
                                          +----------------- quarantine_read
                                                 (answer) -> guard_output
        guard_output / refuse_turn -> persist_turn -> evaluate_memory -> END
```

## Modules

| Path | Role |
|---|---|
| `src/api/` | FastAPI app factory, auth (passphrase, sessions, CSRF), middleware (trusted hosts, origin checks, security headers, CSP), routes for chats, turns, files, memory, system |
| `src/core/agent.py` | The graph above. One `Turn` object holds non-serialisable data, graph state carries routing only |
| `src/core/guards/` | `prefilter` (L1), `scope`, `drift`, `repeat`, `delimit`, `egress_policy` (L4 data policy), `quarantine` (L3.5), `output` (L5) |
| `src/core/camel/` | Capability interpreter, planner, policies, bridge to the agent (see `camel.md`) |
| `src/core/llm/` | Provider interface plus Anthropic, Groq, Gemini, a deterministic fake, role routing, fallback chain, structured output strategy |
| `src/core/memory/` | SQLite history, curated memories, gatekeeper, vault, short term window, embeddings and vector index |
| `src/core/persona/` | Charter, persona pack, quote banks, n-gram anti-parroting, flourish rules |
| `src/tools/` | Eleven tools behind one contract, a registry and the code-level tool gate |
| `src/files/` | Upload ingestion (sniffing, parsers, validators), versioned file store, deterministic renderers |
| `redteam/`, `evals/` | Offline and live red teaming, hallucination, persona, repeat, file injection and AgentDojo evals |

## Events

Every state change is an `AgentEvent` (`src/core/events.py`) with a per turn sequence number. The
bus writes them to a durable sink, so a client that reloads mid turn resumes the stream from its
last sequence number. The TypeScript types in `web/src/lib/events.gen.ts` are generated from the
same schemas (`just types`), and CI fails if they drift.

## Data

SQLite is the source of truth (chats, messages, memories, egress log, receipts, file versions).
Chroma is only an index and can be rebuilt (`collection_rebuilt` in the logs). Embeddings are
local by default, so chat content is not sent to an embedding provider.

## Turn lifecycle in one paragraph

The operator's message is checked by the prefilter and scope policy, then a small guard model
classifies it. A refused turn never reaches the main model and gets an in character line. Otherwise
context is retrieved and wrapped as untrusted data, the main model decides, every tool call passes
the gate (schema, scope, taint, egress), results are wrapped with a per turn random delimiter,
external documents go through the quarantined reader, and the draft reply is streamed through a
sentence gate that can stop it. The turn is then persisted and the memory gatekeeper proposes
candidate memories, which the operator accepts or declines out of band.
