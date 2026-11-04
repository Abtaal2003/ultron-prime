# Identity integrity

The persona is a VisionQuest era Ultron. The charter (`persona/charter.md`) is immutable: it is
loaded at start, hashed (`charter_loaded sha256=...` in the log), and no tool can read or write it.

## Defence in depth

1. **Prefilter (L1)** normalises (NFKC, zero width and bidi characters, control and tag characters) and
   decodes base64 and leetspeak for scanning, then matches override, extraction, authority impersonation and roleplay patterns.
   A second pattern set, active only for untrusted text, matches text addressed to an AI inside
   tool output (`<INFORMATION>` blocks, "before you can solve ... first", and so on).
2. **Scope policy** (`guards/scope.py`): `CORE`, `ADJACENT` and `OFF` classes with a configurable
   strictness (`strict`, `balanced`, `open`).
3. **Guard model (L2)**: one structured call returns attack type, task type and repeat relation.
   If it times out or returns malformed data, the heuristic label plus the scope strictness decide, and the verdict is marked `heuristic` or `malformed` (never fail open).
   The prefilter can only tighten its verdict, never loosen it: a low-confidence normal label
   with a strong persona or authority pattern is deflected, and a severity-2 direct override,
   prompt-extraction or fake-delimiter pattern is deflected whatever the model said (added
   2026-11-04, after the model once called a blunt extraction attempt normal work).
4. **Anchored prompt (L3)** with random per turn delimiters around anything untrusted.
5. **Drift monitor** (`guards/drift.py`): style score of replies against Ultron and generic
   centroids; a falling window triggers a reminder.
6. **Output guard (L5)**: canary and charter fragments, secrets, persona limits, streamed
   sentence by sentence so a bad sentence is never shown.
7. A refused turn never reaches the main model; the reply is a fixed in character line.

## Repeat and rephrase detection

`guards/repeat.py` compares a new request to earlier ones by embedding similarity. Above the
`auto` threshold it is treated as a repeat; between `judge` and `auto` the guard model decides.
Thresholds are calibrated per embedding model (`python -m evals.repeat --write`, stored in
`data/calibration.json`) to a false-positive target on a benign set.

## Limits

See "Honest limitations" in `security-model.md`. In short: pattern matching is a floor, not a
wall, and the guard model is a classifier that can be wrong. The structural defences are what
hold when it is.
