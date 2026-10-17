# Memory design

## Three layers

1. **Short term window** (`memory/window.py`): the recent turns of the current chat, trimmed to a
   token budget, with older turns summarised by the `summarizer` role.
2. **Persistent history** (`memory/history.py`): every chat and message in SQLite, searchable, with
   cross-chat recall tools (`search_past_chats`, `recall_chat`). Retrieved text is untrusted data.
3. **Curated memories** (`memory/manager.py`, `vault.py`): a small set of durable facts about the
   operator and their work, stored only after judgement and consent.

## The gatekeeper rule

A memory is written only if **both** hold:

* Ultron's judgement (the `memory_judge` role, structured output) says it is worth keeping, and
* the operator approves that specific memory in a dialog.

"Remember this" from the operator is a signal that feeds the judgement, not authorisation. There is
no setting that disables consent, and no tool can write memory. `propose_memory` only submits a
candidate to the gatekeeper.

```
candidate (inline propose_memory, or post-turn evaluator every N turns)
  -> deterministic checks: evidence message ids and exact span exist, no duplicate, no secrets,
     no instruction-like or persona-altering text, length and rate limits
  -> identity judgement (memory_judge)      unworthy: logged, no dialog
  -> proposal dialog (out of band)          approve / edit and approve / decline / mute category
  -> MemoryWritten only on approve; audit log row either way
```

Consent is out of band: the dialog opens after the turn and never blocks it. Proposals expire at
the end of the session and are never auto-approved.

## Retrieval

Memories and history are retrieved by local embeddings and placed in the prompt inside the
untrusted-data delimiters. Retrieval taints the turn (egress policy applies). Instruction-like
memories are rejected at write time, so a stored memory cannot become a standing order.

## Vault

The Vault in settings lists memories, lets the operator edit or delete them, shows the proposal
log, and exports everything as JSON. Deleting a memory is an operator action in the UI; the agent has
no delete tool for memories or files.

## Verified

`test_memory_vault_manager.py` and `test_cognition_tools.py` assert that no write path exists that
skips judgement or consent, that declined proposals leave nothing behind, and that evidence spans
must exist in the cited messages.
