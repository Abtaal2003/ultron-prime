# 15. Quarantined reader and its text-to-text limit

Status: accepted

## Context

Models that read documents must not also hold tools.

## Decision

A tool-less reader returns a schema-bound summary. The acting model sees the summary and an indicator, with the original available to the operator on demand.

## Consequences

A fooled reader can pass on a persuasive summary. The schema limits shape and length, not truth.
