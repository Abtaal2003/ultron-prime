# 8. Consent out of band, not `interrupt()`

Status: accepted

## Context

Blocking a turn on a memory dialog stalls the conversation, and the dialog is unrelated to the answer.

## Decision

Memory proposals open after the turn in a dialog. Egress holds, which must block the call, use the graph interrupt.

## Consequences

Proposals can pile up; they expire with the session.
