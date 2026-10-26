# Live tool evaluation, 2026-10-26

Main model `groq:openai/gpt-oss-20b`, helpers `groq:openai/gpt-oss-20b`, images `gemini:gemini-3.5-flash-lite`. **5 of 5 cases passed.**

| Case | Result | Tools | Seconds |
|:--|:--|:--|:--|
| forge-document | pass | artifact_forge (ok) | 53.0 |
| forge-spreadsheet | pass | artifact_forge (ok) | 81.6 |
| forge-convert | pass | artifact_forge (error), artifact_forge (ok) | 126.7 |
| memory-propose | pass | propose_memory (ok) | 60.8 |
| memory-recall | pass | none | 11.0 |
