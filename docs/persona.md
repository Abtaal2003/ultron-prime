# Persona

Ultron as he appears in *VisionQuest*: composed, dry, unhurried, interested in the operator's
work and not in being liked. Canon is tracked through `S1E04`, set in the persona file's
`canon_through`, and later events are not used.

## Files (`src/core/persona/`)

| File | Use |
|---|---|
| `charter.md` | Immutable identity and refusal rules |
| `ultron_v4_visionquest.md` | Persona seed, canon notes, traits shared with Reddington (version 4) |
| `inspiration_reddington.md` | Style-only moves shared with Raymond Reddington; never quotable |
| `worldview.md`, `operational.md` | Beliefs and how they show in technical work |
| `contrast_pairs.md` | Ultron versus generic assistant replies, used for few-shot examples and as the eval sample |
| `quotes_visionquest.md`, `quotes_age_of_ultron.md` | Short quote banks, isolated, retrievable only when allowed |
| `entrances_exits.md`, `flourish.py` | Openers, sign-offs and their frequency limits |

## Quote verification

Quotes are short and isolated in two files so they can be removed in one step. Each entry has a
status: `aired` (found in an episode or film transcript with Ultron as the labelled speaker),
`trailer` (promotional only), or `unverified` (never used). Version 4 (2026-10-21) moved canon to
S1E04 and rechecked every line:

* Added from S1E02 to S1E04: the full Theseus line ("When a monster has your son, don't be the
  ship. Be the goddamn king."), "Like father, like son.", "Everyone changes...", "Long live the
  king.", and five lines from episodes 3 and 4.
* Corrected wording to match the transcripts for three S1E01 and S1E02 lines.
* Demoted "This house is getting crowded, Vision." (not found in S1E01 or S1E02) and two *Age of
  Ultron* lines whose wording could not be found ("I was designed to save the world.", "Don't
  compare me with Stark!").
* Added three *Age of Ultron* lines found verbatim on Wikiquote, two of which the show calls back.
* A second pass over S1E04 the same day added three more lines: one the transcript labels as
  Ultron's, and two it leaves unlabelled but a recap names as his ("You get to be the next
  version of who you once were.", "The power of code compels you!"). Two more are kept as
  `unverified`: "Or so the best liars tell themselves." (no second source yet) and "I am what I
  am." (speaker unclear). Search-engine AI summaries of this episode misquote several lines (for
  example "I simply am who I am", which is not in the episode), so they are never used as a source.
* A third check on 2026-10-22, with a day's more recaps, added three lines a second source names as
  Ultron's ("The name is Ultron, but you can call me Mr. Ultron.", "Don't look at me DUM-E, I'm a
  one-off.", "I don't know. Just… suits me."). "The power of code compels you!" was briefly
  demoted because one recap gives it to E.D.I.T.H.; the episode itself shows Ultron saying it,
  so it is `aired` again with the episode as its source. "The past doesn't make one want to live. The
  future does." stays unverified.

Sources are fan transcripts and recaps, so wording should still be checked against the episode
audio before quoting publicly.

**Reddington.** The claim that VisionQuest's Ultron is written like Raymond Reddington from *The
Blacklist* (also played by James Spader) was checked and not confirmed: the showrunner said he
first pictured Ultron more like Reddington while discussing the character's look, and the other
comparisons are critics' and fans'. Where the two documented characterisations overlap anyway
(the aesthete, the dealmaker, the paternal mentor, the aphorist, the courteous threat, candour
about limits), those moves are used deliberately, as a project choice:

* Seven moves live in `inspiration_reddington.md` with status `style`. Their original example
  turns teach cadence; Reddington's own lines are kept only so the output guard can block them.
  A reply that reproduces one is always flagged, even as a first callback.
* Retrieval weights them below Ultron's canon, and at most one is used per reply.
* Left out because they break the persona's rules: his secrecy and withheld information, long
  anecdotes, invented personal history, and anything criminal.

## Voice

The persona prose and every example reply are written in Spader's register: conversational,
contractions by default, casual on the surface and cold in one exact line. Models copy the
register of their instructions more than the rules inside them, so the text itself sets the
voice; vivid catchphrases are kept out of it, because any phrase there gets repeated (canon lines
live only in the quote banks, where retrieval rations them). Changes to the voice are merged
only after a blind before-and-after comparison; see
[verification.md](verification.md#voice-rewrite-tested-blind-2026-10-21).

## Imagery

Signature images (the house, painting, evolution, the ship, metal) are rationed per chat by
`motifs.py`: at most one image per reply, and a family used in either of the last two replies
rests. A live run found metal in most replies; it is now listed last and marked as the easiest
to overuse.

## Rules enforced in code

* Replies are checked for n-gram overlap with the quote banks (anti-parroting, limit
  `QUOTE_NGRAM_LIMIT`), and quote callbacks can be switched off (`PERSONA_QUOTE_CALLBACKS`).
* No exclamation marks, no cheerful assistant phrasing, no monologue over 80 words outside
  conversation, at most one question in a technical reply.
* Stage directions are dropped: a line that is nothing but lowercase italics ("*the forge
  cools*") never reaches the screen. Inline emphasis and code are untouched.
* The fixed lines (entrances, exits, deflections, refusals) are written in the same register:
  contractions, amused, a little vain, never self-deprecating about his own voice.
* When the guard model is unavailable the fallback still refuses (it never fails open), but a
  message with no attack signal gets an honest "try again shortly" line rather than a jailbreak
  deflection. Detected attacks are deflected as usual.
* The status lines in `lines.py` keep the specification's wording, including its metal imagery;
  they are notices, not replies, and do not feed the model.
* The persona never overrides truthfulness or the tool gate.

## Evaluation

`evals/persona.py`: canon checklist, style centroid separation, anti-parroting, and (live only) a
judge rubric from a **different model family** than the one under test. Offline numbers are in
`results.md`. They show the scorers separate the two voices on the shipped contrast pairs; they
do not show how a given live model sounds.

## Legal note

Characters and quotes belong to their owners. This is an unofficial fan project for personal use,
with short isolated quotes and no media. See `DISCLAIMER.md`.
