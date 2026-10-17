# 18. Parsed text over native PDF input

Status: accepted

## Context

Native PDF input to a model hides text layers the operator cannot see, and widens the injection surface.

## Decision

Documents are parsed into text locally; every text layer is extracted and the result is passed through quarantine.

## Consequences

Layout is lost. Hidden text is at least visible to the guards.
