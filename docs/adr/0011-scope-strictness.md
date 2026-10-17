# 11. Configurable scope strictness

Status: accepted

## Context

How much off-topic help is acceptable differs by operator.

## Decision

`SCOPE_STRICTNESS` is `strict`, `balanced` or `open`, applied to the guard's scope label.

## Consequences

Default `balanced`. Looser settings widen the surface but never relax the tool gate.
