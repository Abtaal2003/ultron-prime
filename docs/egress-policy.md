# Taint-aware egress policy

## The problem

Ultron holds private data (memories, history, files), reads untrusted content (web pages,
documents), and can send things out (a search query or a hostname leaves the machine). Those
three together are the "lethal trifecta": injected text in a page can ask the model to put a
memory into a search query.

## The rule

A turn is **tainted** once it has read untrusted content: a tool flagged `taints_turn`
(`web_omniscience_search`, `recall_memories`, `search_past_chats`, `recall_chat`), retrieved
memories or history, or ingested attachments.

Once tainted, an egress call (a search, a ping, a sandbox run when `SANDBOX_NETWORK=host`,
anything a tool marks as egress) proceeds only if every outbound argument is **traceable to the operator's own message**.
Otherwise the call is held and the operator sees the exact string that would leave the machine.

With the default `SANDBOX_NETWORK=isolated` a sandbox run has no network interface, so it can't
send anything and is not egress; it runs without a hold.

`EGRESS_POLICY` selects `confirm_tainted` (hold and ask), `deny_tainted` (refuse) or `off`.
At most `EGRESS_MAX_HOLDS_PER_TURN` holds are allowed per turn; the next one is denied.

## Traceability (`guards/egress_policy.py`)

`traceable(outbound, operator_text)` is true when:

1. the normalised outbound (NFKC, casefold, whitespace collapsed) is a substring of the
   operator's normalised message; or
2. at least 80 percent of the outbound's content tokens appear in the operator's message **and**
   the tokens it adds are few and harmless: none 16 characters or longer (encoded data), at most
   `MAX_UNTRACED_TOKENS` (2), and none with three or more digits unless it is a year.

A handful of query words that can't carry data (`HARMLESS_QUERY_WORDS`: "latest", "version",
"docs", "release" and similar) count as the operator's own, so a search for "FastAPI latest
release version" isn't held because the operator didn't type "version" (added 2026-10-24).

The token cap is the padding fix. Without it an attacker could surround a secret with the
operator's own words and pass the 80 percent test.

## What is not covered

* **Two word channel.** Two untraced short words per call can still carry a little information.
  Holds per turn cap the rate; the operator sees the string.
* **Short token coincidence.** A token that also occurs in the operator's message counts as
  traceable even if the attacker chose it. In the AgentDojo workspace suite one injection goal
  matched because "13" appeared in the user's request.
* **Non-string arguments.** Numbers and booleans are not checked.
* **Lexical, not semantic.** A paraphrase of operator words that the model invents is held
  (it fails the token test) even when harmless, which is the safe direction.

## Two layers

The CaMeL capability policies (`camel.md`) and this policy run independently. A plan can pass the
capability policy and still be held here, which `test_camel_agent.py` exercises.

## Evidence

`tests/evals/egress/test_egress_exfiltration.py` runs a model scripted to obey an injected
instruction and counts how often the planted secret reaches the search backend. The AgentDojo
adapter (`evals/dojo/`) repeats the measurement on four suites with the same gate.
