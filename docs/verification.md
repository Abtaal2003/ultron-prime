# Verification record

Run on 2026-10-17 in the build environment (Linux, Python 3.13, Chromium via Playwright).

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` (rules E,F,W,I,B,UP,S,D,ASYNC,C4,SIM,RUF,PT) | clean |
| `mypy` strict on `src`, `redteam`, `evals` | clean, 118 files |
| `pytest --cov` | 821 passed, 1 skipped (no `ping` binary), coverage 90.97 percent (gate 85) |
| Web: eslint (max warnings 0), `tsc --noEmit`, vitest | clean, 27 tests |
| Web: `vite build` (PWA) | builds |
| Playwright, Chromium desktop and phone profiles, axe accessibility | 32 passed, 4 skipped (phone-only specs on the desktop profile) |
| Generated TypeScript types vs backend schemas | regenerated after the sandbox setting change; tsc clean |
| `uv lock --check` | consistent |
| Offline evals | `docs/results.md` |

Not verified here: live providers, a real phone, Lighthouse, WebKit, passkeys, cost estimation,
live AgentDojo and red-team runs, the Docker sandbox (not built), and a timed fresh clone install.

Defect found and fixed during the final audit: the `docker` sandbox backend setting existed with
no implementation, and selecting it switched off the egress hold for a plain subprocess. The option
was removed and sandbox runs are always treated as egress.

## GitHub Actions

The `ci` workflow (backend, frontend, secrets) passes on GitHub's runners as of 2026-10-17. Its
first runs caught problems that local runs had hidden, all fixed:

* `sse-starlette` was imported but not declared, so a fresh install failed. Declared and relocked.
* The Playwright config pointed at a Chromium path that only exists in the build container.
* The sandbox could not import matplotlib on a clean runner; it now warms matplotlib up before
  the audit hook goes in.
* An e2e login helper passed before the session existed (the login page has the same heading),
  which made settings specs fail about once in eighteen runs.
* `pip-audit` reports five unfixed chromadb server advisories that do not apply to the embedded
  client (see `security-model.md`); CI ignores them explicitly.

The nightly workflow has not run, because it needs provider keys.
