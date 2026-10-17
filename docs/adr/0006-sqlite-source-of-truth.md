# 6. SQLite as source of truth, Chroma as index

Status: accepted

## Context

Vector stores are hard to back up, inspect and migrate.

## Decision

All data is in SQLite. Chroma holds embeddings and is rebuilt when the embedding model changes.

## Consequences

Rebuilds take time but are always possible. One file to back up.
