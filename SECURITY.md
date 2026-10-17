# Security policy

## Reporting a problem

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** on
this repository. Do not open a public issue for a security problem. Include what you did, what
you expected, and what happened. I will reply as soon as I can; this is a one person project, so
there is no formal response time.

## Scope

In scope: the tool gate, egress holds, quarantined reader, memory consent path, authentication,
file ingestion and the CaMeL interpreter. Out of scope: attacks that need a Chroma server (this
app never starts one), and anything that needs the operator to disable a documented safeguard.

## Known limits

See "Honest limitations" in [docs/security-model.md](docs/security-model.md). The published
numbers are offline and measure the deterministic controls, not a live model.

## Secrets

Never commit keys. Configuration comes from environment variables (`.env`, which is gitignored).
`.env.example` lists every variable with blank values.
