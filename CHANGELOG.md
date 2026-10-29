# Changelog

## 1.0.0 (2026-10-30)

The first release meant for other people to run.

### Run it yourself

* **Docker image and compose file.** One command starts the API, the web terminal and the local
  embedding model, bound to this machine only, with data in named volumes. CI builds the
  image, starts it with compose, checks health, the web app, local embeddings offline and that
  data survives a restart.
* **`ultron doctor`.** Checks keys (never printing them), which model each job will actually use,
  the data and workspace folders, the web build, the embedding model and the sandbox, and says
  how to fix what it finds. `--live` sends one tiny request through each model.
* **Free-tier preset.** `.env.free.example` runs on Groq and Gemini free keys, with requests
  trimmed to Groq's 8,000-token limit.
* **Plain first-start errors.** A missing embedding model or no network to fetch it is one
  sentence with the alternatives, not a traceback. The first-run banner prints the sign-in URL.

### Safety

* **The code sandbox has no network.** On Linux each run gets its own user and network
  namespace, so it is no longer treated as egress and needs no approval; where that isn't
  possible it refuses to run unless you choose `SANDBOX_NETWORK=host`.
* Rules against substitute actions (emptying a file when there is no delete), unasked file
  writes, and obeying instructions planted in uploads, each found in live sessions.

### Tested live

* A 30-case live check that drives every tool through the real pipeline, run on five days on
  free tiers (30, 28, 27, 26 and 27 of 30, every failure fixed and rerun), and four long
  multi-chat sessions. See [docs/verification.md](docs/verification.md).
* `evals/embeddings_check.py` and a manual workflow measure memory recall and calibrate repeat
  detection on real embedding models. The default model (Qwen3-Embedding-0.6B) found every
  related memory with no unrelated ones at the recall cut-off; the small one found under half,
  so Docker and the free preset use the default.

### Fixed on the way

* Sign-in refused on any port but 8787 ("origin not allowed").
* The header named a main model that had no key.
* Oversized requests on Groq's free tier: requests are sized before sending, trimmed in two
  stages, and passed to a fallback with room when the primary still refuses.
* Raw tool fields, receipts and closing offers in replies; invented file contents; memory
  duplicates; one-off requests saved as memories.

Earlier work (17 to 29 October): the guarded agent, CaMeL interpreter, quarantined reader,
egress policy, memory with consent, red team, AgentDojo run, persona v4 and two blind-tested
voice passes. The commit history has the detail.
