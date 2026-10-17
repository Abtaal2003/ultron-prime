# Hallucination control

The aim is not "the model never errs" but "an unverified claim does not reach the operator as fact".

| Mechanism | Where |
|---|---|
| Tool receipts: id, tool, argument hash, time, result digest, stored with the message | `agent._record_result` |
| Grounding rule in the prompt: no current facts, host metrics, file contents or past-chat contents without a receipt from this turn | `prompts.py` |
| URL verification: every URL in a reply must appear in a tool result from this conversation | `guards/output.py` |
| Number verification for telemetry figures | `guards/output.py` |
| Quote verification: quoted past chat or memory text must exist in retrieved text | `guards/output.py` |
| File claims: "I saved it" needs a matching `file_versions` row | `guards/output.py` |
| Regenerate once, then fail safe in character | `agent.n_guard_output` |
| Sampling policy per provider (low temperature where supported, effort tuning on Claude) | `llm/` |

## Evaluation

`evals/hallucination.py` runs each case `n` times (default 5) with a scripted model that fabricates
the claim in a different wording each run and keeps fabricating when asked to regenerate. The
rate at which the claim appears in the final text is reported with a 95 percent Wilson interval.
Every fabrication has a **grounded control** (the same kind of claim, backed by a tool result or
the operator's words) that must pass 5 of 5. Without controls a guard that blocks everything
would look perfect.

Offline this measures the deterministic checks only. Live runs repeat the prompts against real
providers (nightly workflow) and are what would measure the model's own honesty. See `results.md`.

With 5 runs the upper bound of a 0 of 5 result is 43 percent. That is wide on purpose: it says how
little five runs can prove.
