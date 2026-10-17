# 19. Local embeddings by default

Status: accepted

## Context

Chat content should not go to an embedding provider.

## Decision

A local embedding model by default, with a rebuild when it changes. A hashing embedder is used in tests.

## Consequences

Slightly lower quality than hosted embeddings. Thresholds are calibrated per model.
