# 17. Versioned writes instead of delete

Status: accepted

## Context

See the no-delete decision.

## Decision

Every change is a new `file_versions` row with a SHA-256.

## Consequences

Claims of saving a file can be checked against a row.
