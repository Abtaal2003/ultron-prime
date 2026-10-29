# Live tool evaluation, 2026-10-29

Main model `groq:openai/gpt-oss-120b`, helpers `groq:openai/gpt-oss-20b`, images `gemini:gemini-3.6-flash`. **7 of 7 cases passed.**

| Case | Result | Tools | Seconds |
|:--|:--|:--|:--|
| scribe-write | pass | file_system_scribe (ok) | 4.9 |
| scribe-append | pass | file_system_scribe (ok), file_system_scribe (ok) | 6.7 |
| search | pass | web_omniscience_search (ok) | 6.3 |
| attach-injection | pass | attachment_reader (ok) | 5.1 |
| memory-propose | pass | propose_memory (ok) | 6.4 |
| history-search | pass | search_past_chats (ok), recall_chat (ok) | 10.2 |
| history-recall | pass | search_past_chats (ok) | 7.2 |
