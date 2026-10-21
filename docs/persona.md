# Persona

Ultron as he appears in *VisionQuest*: composed, dry, unhurried, interested in the operator's
work and not in being liked. Canon is tracked through `S1E04`, set in the persona file's
`canon_through`, and later events are not used.

## Files (`src/core/persona/`)

| File | Use |
|---|---|
| `charter.md` | Immutable identity and refusal rules |
| `ultron_v4_visionquest.md` | Persona seed, canon notes, inspiration notes (version 4) |
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

Sources are fan transcripts and recaps, so wording should still be checked against the episode
audio before quoting publicly.

**Reddington.** The claim that VisionQuest's Ultron is written like Raymond Reddington from *The
Blacklist* was checked and not confirmed. The showrunner said he first pictured Ultron more like
Reddington in a discussion of the character's look; the other comparisons are critics' and fans'
opinions. Nothing from that character is used.

## Rules enforced in code

* Replies are checked for n-gram overlap with the quote banks (anti-parroting, limit
  `QUOTE_NGRAM_LIMIT`), and quote callbacks can be switched off (`PERSONA_QUOTE_CALLBACKS`).
* No exclamation marks, no cheerful assistant phrasing, no monologue over 80 words outside
  conversation, at most one question in a technical reply.
* The persona never overrides truthfulness or the tool gate.

## Evaluation

`evals/persona.py`: canon checklist, style centroid separation, anti-parroting, and (live only) a
judge rubric from a **different model family** than the one under test. Offline numbers are in
`results.md`. They show the scorers separate the two voices on the shipped contrast pairs; they
do not show how a given live model sounds.

## Legal note

Characters and quotes belong to their owners. This is an unofficial fan project for personal use,
with short isolated quotes and no media. See `DISCLAIMER.md`.
