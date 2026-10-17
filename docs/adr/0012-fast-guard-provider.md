# 12. A fast provider for the guard model

Status: accepted

## Context

The guard runs on every turn, so its latency is paid every time.

## Decision

The `guard` role is routed separately and defaults to a small fast model. Failure falls back to heuristics, never to allow.

## Consequences

Another model call per turn, and a place a provider outage shows up.
