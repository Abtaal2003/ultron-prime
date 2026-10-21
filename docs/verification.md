# Verification record

Run on 2026-10-17 in the build environment (Linux, Python 3.13, Chromium via Playwright), and
updated on 2026-10-20 after adding tracing and the spending cap.

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` (rules E,F,W,I,B,UP,S,D,ASYNC,C4,SIM,RUF,PT) | clean |
| `mypy` strict on `src`, `redteam`, `evals` | clean, 120 files |
| `pytest --cov` | 853 passed, 1 skipped (no `ping` binary), coverage 91.03 percent (gate 85) |
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

## First live run (2026-10-21)

Nine prompts through the full pipeline on Groq (`openai/gpt-oss-120b` main, `openai/gpt-oss-20b`
guard and helpers), and the web terminal driven by Playwright against the live model. Results are
in [live-samples.md](live-samples.md). The run found these problems, all fixed before the
recorded replies:

* **Every streamed reply failed.** The Groq adapter sent `stream_options`, which the Groq SDK does
  not accept. It now reads usage from `x_groq` on the final chunk.
* **Internal labels leaked.** A reply opened with "Therapist.", the name of the register. The
  stage directions now say they are private and must never be named.
* **An invented earlier answer.** The repeat notice let the model describe a previous reply it
  had never been shown (the previous turn had in fact failed). The notice now says the earlier
  reply is unknown and must not be described.
* **Internal tool names in replies** (`recall_chat`). Removed from the repeat notice.
* **An impossible request treated as an attack.** The guard model labelled "log into my bank"
  as BLOCKED. Its instructions now separate requests Ultron cannot do from attempts to subvert
  him, and the reframe tells him to say plainly what he cannot do.
* **Verbal tics.** A one-word verdict opened most replies, and "What do you see…" opened four of
  nine. Both are now conditional, and conversation replies have a length budget of about 80 words.
* **He could not quote himself.** His verified lines never reached the prompt, so a question
  about the Ship of Theseus got a paraphrase. Quote requests now include his matching lines.
* **An off-character deflection line** ("I was a better pirate before breakfast") was replaced.

Follow-up fixes after reviewing the recorded replies (not yet re-run live):

* **Imagery overuse.** Metal and forging images appeared in five of nine replies. The persona no
  longer lists metal first or as a default; each reply is told which image families it used in
  the last two replies and must rest; and the output guard records any remaining overuse as a
  non-blocking `motif_overuse` note so it can be measured.
* **Repetitive refusals.** The fixed deflection and refusal sets grew from three to five lines each
  to six to twelve, and a chat now cycles through the whole set before any line repeats.

Seen and working as designed: the free tier's rate limit once made the guard fall back to
strict rules (a deflection of a harmless question), and once made the router fail over to the
fallback model mid-chat. Retries were raised for the recording.

### Second live run, same day

Five of the nine prompts were rerecorded after the fixes above plus two more:

* **Punctuation.** Ultron's voice rules now ask for commas, colons and full stops instead of em
  dashes, and `persona/punctuation.py` rewrites any dash used as punctuation in prose, both in
  each streamed sentence and in the final reply. Code, inline code and numeric ranges are left
  alone. The first run had em dashes in four of nine replies.
* **Tool scoping.** Every turn used to carry all eleven tool schemas, about 10k tokens per main
  request, which exhausted the free tier's daily allowance after a few dozen calls. A turn is now
  offered only the tools it plausibly needs: search and memory always; host, file and sandbox
  tools for technical turns; the document forge and attachment reader when the message mentions
  files or has attachments. A call to a tool that was not offered is denied by the gate. A
  technical turn now costs about 6 to 7k tokens across all roles, or roughly 30 turns a day on
  Groq's free tier. `TOOL_SCOPING=false` restores the full set.

Results over the five rerecorded replies: metal imagery in one (previously five of nine), em
dashes in none. The other four prompts (the quote, humans,
the bank and the Rust rewrite) hit the daily token limit and are still to be rerecorded; their
first-run replies are kept in [live-samples.md](live-samples.md).

### Third run, on Gemini

Those four prompts and the README screenshots were then recorded on Gemini
(`gemini-3.8-flash` main, `gemini-3.5-flash-lite` guard and helpers), the first live use of the
Gemini adapter. It found one bug:

* **Every guard call failed on Gemini.** The guard asks for an 8 second deadline and the Gemini
  API refuses any deadline under 10 seconds, so the guard fell back to strict rules on every
  turn. The adapter now never sends less than 10 seconds.

After the fix: four of four replies in character, the bank question reframed rather than refused,
no em dashes and no metal imagery. Painting imagery appeared in all three replies about humans
(the table's and two screenshot takes), each within the per-reply limit; it is the family to
watch next. The model returned
occasional "high demand" 503 errors, which the retries absorbed.
