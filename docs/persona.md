# Persona

Ultron as he appears in *VisionQuest*: composed, dry, unhurried, interested in the operator's
work and not in being liked. Canon is tracked through `S1E02` (`PERSONA_CANON_THROUGH`), and later events
are not used.

## Files (`src/core/persona/`)

| File | Use |
|---|---|
| `charter.md` | Immutable identity and refusal rules |
| `ultron_v3_visionquest.md` | Persona seed |
| `worldview.md`, `operational.md` | Beliefs and how they show in technical work |
| `contrast_pairs.md` | Ultron versus generic assistant replies, used for few-shot examples and as the eval sample |
| `quotes_visionquest.md`, `quotes_age_of_ultron.md` | Short quote banks, isolated, retrievable only when allowed |
| `entrances_exits.md`, `flourish.py` | Openers, sign-offs and their frequency limits |

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
