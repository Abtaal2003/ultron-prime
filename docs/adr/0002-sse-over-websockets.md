# 2. Server-sent events over WebSockets

Status: accepted

## Context

Turns are one-way streams from server to client; the client's only messages are a few small POSTs (stop, hold decision).

## Decision

Events stream over SSE with a per turn sequence number and `Last-Event-ID` resume. Commands are ordinary POSTs.

## Consequences

Works through simple proxies, reconnects natively, and a reload mid turn resumes cleanly. No bidirectional channel to secure.
