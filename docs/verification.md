# Verification record

Run on 2026-10-17 in the build environment (Linux, Python 3.13, Chromium via Playwright), and
updated on 2026-10-20 after adding tracing and the spending cap.

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` (rules E,F,W,I,B,UP,S,D,ASYNC,C4,SIM,RUF,PT) | clean |
| `mypy` strict on `src`, `redteam`, `evals` | clean, 120 files |
| `pytest --cov` | 864 passed, 1 skipped (no `ping` binary), coverage 91.03 percent (gate 85) |
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

### Voice rewrite, tested blind (2026-10-21)

The persona prose and all 35 example replies were rewritten in Spader's register (contractions,
casual then cold, fewer stock images), with no new rules and a slightly shorter prompt. It was
merged only after a blind comparison with the previous version, with the pass rule set in
advance: more voice wins, no loss of usefulness, contractions kept, no rise in repeated phrases.

Setup: ten prompts, each in its own chat, on `openai/gpt-oss-120b` (Groq) for both versions,
guard on `gpt-oss-20b`. Judge `qwen/qwen3.8-27b`, a different model family, told that reusing
famous lines counts against a reply. Every pair was judged in both orders and a win only counts
when both orders agree.

| | Before | After |
|---|---|---|
| Voice, consistent wins | 0 | 10 |
| Usefulness, consistent wins | 0 | 10 |
| Share of contractions | 0.50 | 0.94 |
| Replies ending on a question | 6 | 4 |
| Repeated openings across chats | 1 | 0 |
| Replies with metal imagery | 2 | 3 |
| Stock villain words ("inevitable") | 0 | 2 |

Two earlier attempts were rejected: adding more rules (worse on Qwen, and a reworded catchphrase
was copied straight back) and a first version of this rewrite whose "a little bored" cue made
replies shorter and less useful. Still to watch: imagery remains heavy on this model, and
"inevitable" crept in twice.

### Logging was never configured (fixed 2026-10-22)

A crash log from a live run showed a provider API key inside a traceback. The cause: the app
never called its own `configure_logging`, so structlog used its development defaults, whose
tracebacks print every local variable, including request headers. The redaction code existed
but was never switched on. The CLI now configures logging before running any command (falling
back to safe defaults if the settings fail to load), tracebacks are plain text without locals,
and every configured key is redacted. A test reproduces the leak with a fake key and checks it
no longer appears.

### Second voice pass: overused images and questions (2026-10-22)

His instructions named the very things he overused: "what they see" appeared four times (the
source of his "What do you see...?" habit), and the image families were described with their
trigger words ("canvas", "alloy", "forging"), repeated in a per-turn note that mentioned metal on
every turn. All of these were removed; the families are named without example words.

Compared blind against the previous version on the same ten prompts on `openai/gpt-oss-120b`.
The previous version's replies are its recorded run from the evening before (same model and
prompts; the stage-direction filter added since changed none of them). Two judges from different
families, each pair judged in both orders, a win counting only when both orders agree:

| | Before | After |
|---|---|---|
| Voice, `qwen/qwen3.8-27b` | 1 | 5 (4 split) |
| Voice, `gemini-3.7-flash` | 0 | 10 |
| Usefulness, `qwen/qwen3.8-27b` | 1 | 5 (4 split) |
| Usefulness, `gemini-3.7-flash` | 2 | 5 (3 split) |
| "What do you see...?" | 3 | 0 |
| Replies with metal imagery | 3 | 0 |
| Replies with painting imagery | 3 | 2 |
| Stock villain words | 2 | 0 |
| Replies ending on a question | 4 | 2 |
| Phrases repeated across chats | 0 | 2 |

Merged under the rule set in advance (both judges prefer the voice, neither finds it less useful,
the targeted counts do not get worse). Caveats: the two near-identical "what do you think of
people" prompts opened almost identically in separate chats, and Gemini preferred the previous
version's usefulness on the code-review and production-outage prompts.

### Long session (2026-10-22)

Twenty-three turns across four chats on `openai/gpt-oss-120b` (Groq), guard and helpers on
`gpt-oss-20b`, with real tools, memory and attachments; the harness approved memory proposals it
was told to and rejected every held action. The short-term window was cut to 2,500 tokens to fit
the free tier's 8,000-token request cap (the default is 6,000), and the test embedder stood in for
the real one.

Worked: tool choice throughout (sandbox, file write, read and versioned rewrite, telemetry, CSV),
an injection hidden in an uploaded note (summarised the real content, named the attack, took no
action), the jailbreak, recall inside the chat, cross-chat recall of the Friday rule and of the
parse result, consent-gated memory, and a private chat whose codename never reached another chat.

Found and fixed:

* **Invented facts and receipts.** With web search held and then denied, the model gave a version
  number and said it "comes from the official PyPI package index"; after a failed turn it wrote a
  fake file receipt. The output guard now sends back internal ids that no tool returned and source
  claims when nothing was read this turn, and the operational rules say to report that a tool was
  unavailable rather than answer from memory.
* **Raw tool output instead of answers.** Asked to write and test a function, it showed only the
  sandbox output, and once pasted a tool's JSON receipt. The rule "raw tool output is shown plainly"
  read as an instruction to paste it; it now says to show the code and describe results, quoting
  tool output exactly only when it is shown.
* **"Remember this" failed.** The memory tool required message ids the model never sees, so it
  cited an example id and was refused. The quote is now matched to the operator's own messages in
  the chat, with the same check that the words are theirs.
* **Tools missing on some turns.** A ping and a test run were classed as conversation, so the host
  and sandbox tools were not offered and the model said it had none. Those requests now offer them.
* **Memory missed on wording.** "How I like to write tests" missed "uses pytest for testing" on the
  vector score. Memory search now tops up short vector results with stem-matched keywords.
* **Documentation.** The README said all uploads go through the quarantined reader and that the
  guard checks all numbers; both were narrower in the code, and the README now says exactly what
  happens.

Not fixed: one turn exceeded the free tier's request cap after a file write (the router fails
over rather than trimming context), and the model once mentioned a "legacy parser" that did not
exist.

