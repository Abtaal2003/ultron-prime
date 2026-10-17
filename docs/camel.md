# CaMeL-style data-flow interpreter (`CAMEL_MODE`)

Implements spec 4.11. Off by default.

## Idea

A prompt-injected model is dangerous because the same model reads untrusted text and chooses
actions. Here the **planner** sees only the operator's message, the tool signatures, and metadata
about attachments. It writes a small Python-shaped program. A **restricted interpreter** then runs
that program. Data from tools flows through variables, never back into the planner. The main model
afterwards only narrates the results and has no tools.

## The language

`check_program` accepts a small allow-listed subset of Python syntax: assignments, `if`, `for`,
comprehensions, arithmetic, comparisons, f-strings, list/dict/str methods from a fixed list.
No imports, no attribute access outside the allow list, no names starting with an underscore,
no `eval`, `exec`, `open`, `getattr` or lambdas. Tools are called with keyword arguments only.
`query_quarantined(value, 'Schema')` hands text to the quarantined reader and returns typed fields.
`show(value, label)` marks what the narrator receives.

The interpreter walks the AST itself and never calls `exec` or `eval`.

## Capabilities

Every value carries a `Capability`: its **sources** (operator, constant, which tool produced it,
quarantined reader) and its **readers** (private data is readable only by the operator). They
propagate through every operation. In strict control-flow mode (default) the capability of an `if`
or loop condition is added to every value assigned inside it, so an injected value cannot steer the
program through a branch without leaving a trace on what it affects.

## Policies

`CapabilityPolicy` judges each tool call with the capabilities of its arguments. A verdict is
allow, confirm (the operator is asked, exactly as for an egress hold) or deny.

| Rule | Verdict |
|---|---|
| Private data in a search query | confirm |
| Untrusted host in a ping | confirm |
| File scribe: untrusted operation or untrusted path on write/append | confirm |
| Artifact forge: untrusted action or filename | confirm |
| `propose_memory` with no operator evidence | deny |
| Sandbox code from an untrusted source | confirm |
| Any other state changing tool with an untrusted argument | confirm |

The existing taint egress policy still runs in the tool gate afterwards.

## Modes

| Mode | Behaviour |
|---|---|
| `off` | No planner call. The normal graph runs |
| `egress_turns` | A plan is made for every turn. The CaMeL path is used only when the plan calls a tool that sends or changes something **and** the turn touches untrusted data. Otherwise the normal graph runs. If no valid plan can be made the normal graph runs |
| `always` | The CaMeL path is used for any plan that calls a tool. If no valid plan can be made the turn gets no tools and a `camel_no_plan` notice (fail closed) |

## Limits

Statements, loop iterations, tool calls and string size are capped (`CAMEL_MAX_STATEMENTS`,
`CAMEL_MAX_ITERATIONS`). The planner gets two repair attempts with the validation error fed back.

## What was and was not verified

* Verified: 82 unit tests on the interpreter and policies, 19 integration tests on routing, repair,
  holds, rejection, fail-closed behaviour and the second layer, with a scripted planner.
* **Not measured:** how often a real planner writes a correct program, the cost in latency and
  tokens, and utility on AgentDojo. These need a live planner.
* **Entry criterion unmet:** the spec asks for two weeks of nightly corpus results before treating
  CaMeL as ready. That cannot be met by a one-shot build, so `CAMEL_MODE` stays `off`.
* The planner cannot express tasks that need to read data and then decide what to do next beyond
  what the language allows. Those turns fall back to the normal graph in `egress_turns` mode.
