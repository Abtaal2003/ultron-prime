# 14. Taint-aware egress first, CaMeL second

Status: accepted

## Context

CaMeL is the stronger design but needs a planner, an interpreter and weeks of corpus before it can be trusted.

## Decision

Ship the taint-aware egress policy as the default, with hooks for capability policies. CaMeL is built behind `CAMEL_MODE`, off by default.

## Consequences

Two layers can disagree, which is intended: either can hold a call.
