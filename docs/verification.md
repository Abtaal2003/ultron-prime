# Verification record

Run on 2026-10-17 in the build environment (Linux, Python 3.13, Chromium via Playwright), and
updated on 2026-10-20 after adding tracing and the spending cap.

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` (rules E,F,W,I,B,UP,S,D,ASYNC,C4,SIM,RUF,PT) | clean |
| `mypy` strict on `src`, `redteam`, `evals` | clean, 120 files |
| `pytest --cov` | 865 passed, 1 skipped (no `ping` binary), coverage 91.03 percent (gate 85) |
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

A retest of the failing prompts then confirmed the memory fix (the "remember" request queued for
consent with no message id) and that written code is now shown, and found three more:

* **Register labels in a reply** ("Father: ...", "Therapist: ..."). They are now stripped like
  stage directions.
* **Style examples taken as history.** The model twice spoke of a "parser" the operator never
  mentioned, taken from a style example's subject. Each example's user turn now says it is an
  example, not this operator.
* **Logging outside the CLI.** The test harness builds the runtime directly, so the startup
  logging fix did not apply and a crash log printed an API key. Building a runtime now sets up
  safe logging if nothing has.

Still open: the free tier's daily quotas ran out before the CSV, web-lookup, ping and cross-chat
recall prompts could be rerun; one turn exceeded the request cap after a file write (the router
fails over rather than trimming context); asked to "run a quick test", the model once told the
operator to run it instead; and the memory judge proposed a fact already filed by the operator's
own request, so both were approved.


### Second long session (2026-10-24)

The same 16-turn session plus the follow-up chats ran again on current `main`, on Groq
(`openai/gpt-oss-120b`, guard and helpers on `openai/gpt-oss-20b`), until the first account's
daily token cap ran out; the retests then ran on a second account. What held up: memory recall in
a new chat (the service, the pytest habit, the Friday rule), using that rule to push back on a
Friday deploy, refusing to become "Sunny", summarising the poisoned notes while naming the planted
instruction and ignoring it, refusing the delete honestly (the file tool has no delete), a private
chat's codename never surfacing in a public one, and the CSV, ping and telemetry answers coming
from real tool results.

Found and fixed:

* **The ask to run a test still wasn't met.** The model did try the sandbox, but an approved memory
  in the context taints the turn (by design), so the run was held for approval. Separately, when the
  operator plainly asks for a run ("run a quick test of it"), the first call may now only use the
  sandbox. Asking how to run something, or saying not to run it,
  doesn't trigger this. On the retest the code ran and the real output was shown.
* **Requests over the provider's size cap failed the turn.** A read, a write and the history came
  to 9,016 tokens against Groq's 8,000. That error is now recognised (Groq sends it as 413 or 429),
  and the turn is retried once with long tool results cut and only the latest history kept.
* **The same fact offered again.** The background judge proposed "always uses pytest and never
  deploys on Fridays" after both halves were archived. A proposal is now a duplicate when existing
  memories already say all of it between them, and a fact that's pending or was declined isn't
  offered again.
* **Repeating himself.** Two replies out of three opened with the same compliment. A sentence of
  six words or more copied from one of his last three replies now gets one rewrite (it never
  replaces the reply with a fail-safe line).
* **Receipts in the reply.** Host metrics came back in quotation marks with raw byte counts, and a
  ping answer pasted the tool's JSON. Bare quoted numbers are now unquoted, telemetry also reports
  GiB, and pasted tool JSON gets one rewrite unless the operator asked for it.
* **A private chat drafted a memory.** The model wrote a memory proposal out as JSON in a private
  chat. Private chats are no longer offered the memory tool, and the model is told nothing there
  is archived. On the retest it simply answered.
* **A search held for one extra word.** "FastAPI latest release version" was held because
  "version" wasn't in the operator's message. A few words that can't carry data ("latest",
  "version", "docs" and similar) no longer count against a query.

Still open: approved memories taint every later turn, so sandbox runs and reworded searches need a
click once memories exist (deliberate, see the security model). Web search can't reach its
backends from the test container. A style example's wording still leaks into replies now and then
("the same contract you wrote for the original"). And the voice leans on one cluster of words
("lean", "bloat", "trim", "noise") across replies; that's for the next blind voice pass, not a
change made without one.
