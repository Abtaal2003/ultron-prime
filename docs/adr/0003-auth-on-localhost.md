# 3. Authentication even on localhost

Status: accepted

## Context

Any web page open in the operator's browser can send requests to `127.0.0.1`. DNS rebinding turns that into reads.

## Decision

Passphrase login, session cookies (`SameSite=Strict`), CSRF tokens, `Origin` checks and a trusted-host allow list on every route in every access mode.

## Consequences

A first run prints a passphrase. This costs a login but closes drive-by and rebinding attacks.
