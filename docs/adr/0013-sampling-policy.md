# 13. Sampling policy instead of a temperature setting

Status: accepted

## Context

Some current Claude models do not accept sampling parameters, and others do.

## Decision

A `SamplingPolicy` per call, applied only where the model supports it; effort tuning and structured outputs elsewhere.

## Consequences

Results are rates with intervals, not single pass or fail.
