# 10. Subprocess sandbox; Docker not built

Status: accepted

## Context

Running code safely needs isolation, but Docker may not be available.

## Decision

A subprocess with rlimits, a scrubbed environment and a Python level network block. Because that is not isolation, every run is treated as egress and held when the turn is tainted. The Docker option in the spec was not built, and the setting was removed so it cannot be selected by mistake.

## Consequences

Weaker than a container. The honest label is in `security-model.md`.
