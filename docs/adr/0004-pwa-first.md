# 4. PWA first, native wrapper later

Status: accepted

## Context

Phone access matters, a store app does not yet.

## Decision

Ship an installable PWA with an app-shell service worker that caches no private data. A Capacitor wrapper is left for later.

## Consequences

No store review or signing. Offline shows the shell only. Push and background work are limited by the browser. Tested on Chromium emulation, not on a real phone.
