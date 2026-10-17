# 9. No delete tool

Status: accepted

## Context

A confused or hijacked agent should not be able to destroy data.

## Decision

Writes create new versions. Originals are immutable. Deleting is an operator action in the UI.

## Consequences

Storage grows. Recovery is always possible.
