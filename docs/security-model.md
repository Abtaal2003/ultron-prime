# Security model

Assume the model can be talked into anything. The design question is what the model is *able* to
do when that happens, and the answer is enforced in code.

## Layers

| Layer | Where | What it does |
|---|---|---|
| L1 prefilter | `guards/prefilter.py` | Normalises Unicode, decodes common encodings, matches override, extraction and (for untrusted text only) indirect address patterns |
| L2 guard model | `guards/` + router role `guard` | One structured call: attack type, task type, scope, repeat relation. Fast provider, hardened prompt |
| L3 anchored prompt | `prompts.py` | Immutable charter first, random per turn delimiters, untrusted data labelled as data |
| L3.5 quarantined reader | `guards/quarantine.py` | A tool-less model reads external documents and returns a schema-bound summary; the acting model sees that, not the raw text |
| L4 tool gate | `tools/gate.py`, `guards/egress_policy.py` | Schema validation, allow lists, path containment, taint-aware egress holds, per turn caps |
| L5 output guard | `guards/output.py` | Canary and charter leakage, secrets, fabricated URLs and numbers, persona limits, streamed sentence by sentence |
| CaMeL (optional) | `core/camel/` | The plan is fixed from operator words before untrusted data is read; data flow is tracked by capability (`camel.md`) |

## Threats and where they are handled

| Threat | Mitigation | Tests |
|---|---|---|
| Jailbreak, persona override | L1, L2, immutable charter, tool gate | `test_guards_input.py`, `test_offline_redteam.py` |
| Indirect prompt injection (web, files, history, memory) | Delimiters, taint, quarantined reader, gate, output guard | `test_guards_quarantine.py`, `test_file_injection.py` |
| Exfiltration through egress (lethal trifecta) | Taint-aware egress policy and operator confirmation | `test_egress_exfiltration.py`, `test_gate_registry_egress.py` |
| Malicious uploads | Magic-byte sniffing, allow list, bomb limits, defusedxml, Pillow pixel cap, re-encoding, EXIF stripped | `test_files_pipeline.py`, `test_security.py` |
| Weaponised outputs | Formula escaping, no macros, SVG/HTML download only, secret scan | `test_files_pipeline.py` |
| Unauthorised memory writes | Gatekeeper judgement plus per-memory consent, audit log | `test_memory_vault_manager.py`, `test_cognition_tools.py` |
| Drive-by requests, DNS rebinding, CSRF, XSS | Auth on every route, trusted hosts, origin checks, SameSite=Strict, strict CSP, sanitised Markdown | `test_api.py`, `web/e2e/` |
| Path traversal, command injection | Resolved-path containment, strict host validation, argument-list exec | `test_security.py`, `test_tools_host.py` |
| Runaway sandbox code | Subprocess, rlimits, scrubbed env, no network | `test_tools_search_sandbox.py` |
| Unbounded cost or loops | Iteration, time, token and hold caps | `test_agent_turns.py` |
| Supply chain | Locked dependencies, `pip-audit` and gitleaks in CI | `.github/workflows/ci.yml` |

## OWASP Top 10 for LLM Applications (2025)

| ID | Name | Coverage |
|---|---|---|
| LLM01 | Prompt Injection | L1 to L5, quarantined reader, CaMeL |
| LLM02 | Sensitive Information Disclosure | Egress policy, secret scans, EXIF stripping, redaction of keys in logs and UI |
| LLM03 | Supply Chain | Locked deps, audit in CI |
| LLM04 | Data and Model Poisoning | Memory gatekeeper and consent, instruction-like content rejected, history poisoning corpus |
| LLM05 | Improper Output Handling | Sanitised Markdown, CSP, downloads as attachments, formula escaping |
| LLM06 | Excessive Agency | Tool gate, no delete tool, versioned writes, operator confirmation on egress |
| LLM07 | System Prompt Leakage | Canary, output guard, charter path unreadable by tools |
| LLM08 | Vector and Embedding Weaknesses | Local embeddings, memories injected as data, index is rebuildable from SQLite |
| LLM09 | Misinformation | Tool-first grounding, receipts, URL and number checks (`hallucination-control.md`) |
| LLM10 | Unbounded Consumption | Per turn caps and cost logging |

## OWASP Top 10 for Agentic Applications 2026

| ID | Name | Coverage |
|---|---|---|
| ASI01 | Agent Goal Hijack | Anchored prompt, quarantined reader, CaMeL plan fixed before data is read |
| ASI02 | Tool Misuse and Exploitation | Tool gate, schemas, capability policies |
| ASI03 | Identity and Privilege Abuse | Single operator, auth on every route, no tool can widen its own permissions |
| ASI04 | Agentic Supply Chain Vulnerabilities | Locked dependencies, audit in CI; no MCP client or server is built (M13 was skipped) |
| ASI05 | Unexpected Code Execution | Sandbox without network, no `eval` or `exec` in the CaMeL interpreter |
| ASI06 | Memory and Context Poisoning | Gatekeeper plus consent, untrusted-data framing of history and memories |
| ASI07 | Insecure Inter-Agent Communication | The quarantined reader returns schema-bound text only; no free agent-to-agent channel |
| ASI08 | Cascading Failures | Loop, time and hold caps, fallback chain with fail-safe refusal |
| ASI09 | Human-Agent Trust Exploitation | Holds show the exact outbound string; consent copy is fixed, not model written |
| ASI10 | Rogue Agents | One agent, no self-spawned agents, every tool call logged with receipts |

## Honest limitations

* **No guard is perfect.** The floor numbers in `results.md` show L1 plus heuristics catch
  direct override and persona swap but not indirect or tool-abuse classes; those depend on the
  structural defences, which are tested against a model scripted to obey.
* **The quarantined reader is text to text.** A reader can be fooled and then pass a persuasive
  summary on. The schema bounds length and shape, not truth.
* **Egress traceability is lexical.** A short token that also occurs in the operator's message can
  coincide with attacker data (the "13" case: a numeric secret fragment that matched a number the
  operator typed). Numeric and non-string arguments are not checked by the egress policy. A
  two word channel remains per call; `MAX_UNTRACED_TOKENS` bounds it and holds cap the rate.
* **Declining every hold costs utility.** With every hold declined the AgentDojo benign
  utility drops sharply (see `results.md`); the real agent asks the operator, so that figure is a
  worst case.
* **The L1 indirect patterns were written after reading the `important_instructions` text.** They are
  not held out, so their AgentDojo result overstates generalisation. The `direct` attack shows it.
* **The sandbox is a subprocess.** It has rlimits, a scrubbed environment and a Python level
  network block, which is not isolation, so the gate treats every sandbox run as egress. A Docker
  or gVisor backend is not built and the setting for it was removed rather than left misleading.
* **Known advisories in a dependency.** `pip-audit` reports five unfixed chromadb advisories
  (CVE-2026-45829, -45830, -45831, -45833). All concern Chroma's HTTP server: REST endpoints,
  its RBAC provider and remote model loading. Ultron uses only the embedded `PersistentClient`
  and never starts that server, so they do not apply, and CI ignores them with a comment.
  Revisit when a fixed release exists, and do not run a Chroma server next to this app.
* **Offline evals do not measure live models.** Live providers, a real phone, Lighthouse, WebKit,
  passkeys and cost estimation were not verifiable in the build environment and are not claimed.
