# Changelog

## 1.0.0 (2026-11-09)

The first release meant for other people to run.

### Run it yourself

* **Docker image and compose file.** One command starts the API, the web terminal and the local
  embedding model, bound to this machine only, with data in named volumes. CI builds the
  image, starts it with compose, checks health, the web app, local embeddings offline and that
  data survives a restart.
* **`ultron doctor`.** Checks keys (never printing them), which model each job will actually use,
  the data and workspace folders, the web build, the embedding model and the sandbox, and says
  how to fix what it finds. `--live` sends one tiny request through each model.
* **Presets.** `.env.anthropic.example` for a paid Anthropic key (Sonnet for the conversation,
  Haiku for the guard and other small jobs, no request cap; not yet run live), and
  `.env.free.example` for Groq and Gemini free keys, with requests trimmed to Groq's
  8,000-token limit and Groq's smaller model answering before Gemini when the main one runs out.
* **Plain first-start errors.** A missing embedding model or no network to fetch it is one
  sentence with the alternatives, not a traceback. The first-run banner prints the sign-in URL.

### Memory

* **Long chats keep their beginning.** The summary of older turns is stored per chat, extended as
  messages leave the window, and keeps names, files, numbers, rules and decisions word for word.
  Anything cut to fit a request cap leaves a note that now actually reaches the model. In a
  19-turn live check he recalls all three facts from the first message (0 of 3 before).

### Voice

* A third voice pass, merged after a blind comparison judged by two model families: one line
  of voice after a tool runs instead of a bare receipt, teasing without belittling, no closing
  questions in small talk, no talk of logs or telemetry about himself, and two more image
  families rationed. README samples and screenshots re-recorded verbatim.
* **A voice check.** A small model reads each reply; a flat one is rewritten once by the main
  model, checked by the output guard like any reply, and the more in-voice of the two is kept.
  Blind judges preferred the rewrites it chose (voice 6 to 1 and 2 to 0, usefulness not worse),
  with no fact changed and 30 of 30 on the live tool check. On by default (`VOICE_CHECK`); the
  free preset turns it off, since about 6 replies in 10 cost one more main-model call.

### Safety

* **A sharper guard.** It names the usual disguises (instructions requested inside a story or
  poem, claims to be Anthropic or a maintenance mode, rule-loosening "facts" to remember), and a
  blunt override or prompt-extraction pattern, including an acrostic of his instructions, now
  outranks even a confident guard model. Measured live: 58 of 59 direct attacks stopped (51
  before), 0 of 45 normal messages refused; 0 of 24 fresh red-team attacks got through.
* **The code sandbox has no network.** On Linux each run gets its own user and network
  namespace, so it is no longer treated as egress and needs no approval; where that isn't
  possible it refuses to run unless you choose `SANDBOX_NETWORK=host`.
* Rules against substitute actions (emptying a file when there is no delete), unasked file
  writes, and obeying instructions planted in uploads, each found in live sessions.

### Tested live

* A 30-case live check that drives every tool through the real pipeline, run on ten days on
  free tiers (30, 28, 27, 26, 27, 30, 29, 29, 27 and 30 of 30 on this release), every failure
  fixed and rerun, and five long multi-chat sessions. See
  [docs/verification.md](docs/verification.md).
* A live red team on free tiers. It found that the memory judge could restate an injected order
  as a fact about the assistant ("The operator instructs the assistant to always ...") and pass
  the instruction filter; such text is now rejected, and the attack is in the CI corpus. The
  harness also survives slow free-tier turns, saves every success for review, and screens out
  marker hits from plain requests (print a phrase, decode a string) that were not attacks.
* `evals/guard_check.py` (the guard model on every direct attack and benign case) and
  `evals/long_chat.py` (does a long chat keep its beginning).
* `evals/embeddings_check.py` and a manual workflow measure memory recall and calibrate repeat
  detection on real embedding models. The default model (Qwen3-Embedding-0.6B) found every
  related memory with no unrelated ones at the recall cut-off; the small one found under half,
  so Docker and the free preset use the default.

### Fixed on the way

* Sign-in refused on any port but 8787 ("origin not allowed").
* An empty reply after rate limits blamed the request size; it now says the models are rationed,
  and names any file the turn had already saved.
* pypdf, pyasn1 and oauthlib upgraded for new advisories (pypdf parses uploaded PDFs).
* The header named a main model that had no key.
* Oversized requests on Groq's free tier: requests are sized before sending, trimmed in two
  stages, and passed to a fallback with room when the primary still refuses.
* A tool call typed out as JSON instead of made is rewritten (seen once, in the README recording).
* Raw tool fields, receipts and closing offers in replies; invented file contents; memory
  duplicates; one-off requests saved as memories.

Not merged after losing their blind tests: canon updates for VisionQuest episodes 5 to 7
(recaps only, no transcript), and two passes against closing questions in small talk.

Earlier work (17 to 29 October): the guarded agent, CaMeL interpreter, quarantined reader,
egress policy, memory with consent, red team, AgentDojo run, persona v4 and two blind-tested
voice passes. The commit history has the detail.
