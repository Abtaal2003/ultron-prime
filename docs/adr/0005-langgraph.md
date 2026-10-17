# 5. LangGraph for the turn graph

Status: accepted

## Context

The turn has branches (refuse, tools, hold, answer) and an interrupt for egress holds that must survive a reconnect.

## Decision

A `StateGraph` with a checkpointer. Non-serialisable turn data lives outside the state; the state only carries routing values.

## Consequences

Explicit nodes make each guard visible and testable. The cost is a dependency and care about what is checkpointed.
