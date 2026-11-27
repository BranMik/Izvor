# Security & Authentication Architecture

This document details the security posture, authentication protocols, privilege boundaries, atomic HITL state transitions, and cryptographic defenses implemented across **Izvor**.

## Table of Contents

- [Multi-Layer Defense-in-Depth Model](#multi-layer-defense-in-depth-model)
- [Slack Ingress: HMAC-SHA256 Signature Verification](#slack-ingress-hmac-sha256-signature-verification)
  - [1. Timestamp Freshness (Anti-Replay)](#1-timestamp-freshness-anti-replay)
  - [2. Signature Computation & Constant-Time Comparison](#2-signature-computation--constant-time-comparison)
- [Administrative API & Interactivity Pre-Authorization](#administrative-api--interactivity-pre-authorization)
  - [1. HTTP Basic Auth on `/api/*`](#1-http-basic-auth-on-api)
  - [2. Whole-Payload `ActionSpec` Interactivity Gate](#2-whole-payload-actionspec-interactivity-gate)
  - [3. Atomic Single-Statement HITL State Transitions](#3-atomic-single-statement-hitl-state-transitions)
- [Privilege Boundaries, File Neutralization & Prompt Safety](#privilege-boundaries-file-neutralization--prompt-safety)
  - [1. Declarative Admin Authorization SSOT & Dual Gate (`origin == "slash"` + `is_slack_admin`)](#1-declarative-admin-authorization-ssot--dual-gate-origin--slash--is_slack_admin)
  - [2. Slack Upload Extension Neutralization (`SAFE_DOWNLOAD_EXTENSIONS`)](#2-slack-upload-extension-neutralization-safe_download_extensions)
  - [3. PII Masking, Configurable NER Models & Automated Skill Exclusions](#3-pii-masking-configurable-ner-models--automated-skill-exclusions)
  - [4. LanceDB SQL Filter Validation (`_sql_literal`)](#4-lancedb-sql-filter-validation-_sql_literal)
  - [5. Slack Output Sanitization](#5-slack-output-sanitization)
- [Path Traversal, Web SSRF & Sandbox Isolation](#path-traversal-web-ssrf--sandbox-isolation)
- [Log Credential Sanitization & Data Protection](#log-credential-sanitization--data-protection)
- [Semantic Prompt Injection, Indirect Tool Screening & Jailbreak Guardrail](#semantic-prompt-injection-indirect-tool-screening--jailbreak-guardrail)
- [Denial of Service (DoS) Defense & Audit Rate-Limiting](#denial-of-service-dos-defense--audit-rate-limiting)

---

## Multi-Layer Defense-in-Depth Model

Izvor enforces rigorous security controls across all ingress, orchestration, execution, and storage layers:

```mermaid
graph TD
    subgraph Ingress Security
        SlackPOST["Slack Request (/slack/*)"] --> TimeCheck{"Timestamp < 300s?"}
        TimeCheck -->|No| ReplayRejection["HTTP 400: Replay Blocked"]
        TimeCheck -->|Yes| HMACCalc["HMAC-SHA256 Signature Verification"]
        HMACCalc -->|Mismatch| SigRejection["HTTP 401: Invalid Signature"]
        HMACCalc -->|Match| IngressOK["Pass to Event / Interactivity Handler"]
    end

    subgraph Administration, Interactivity & GDPR Security
        AdminReq["API Request (/api/*)"] --> BasicAuth["HTTP Basic Auth Header"]
        BasicAuth --> ConstantTimeCompare{"secrets.compare_digest"}
        ConstantTimeCompare -->|Mismatch| AuthFailAudit["Log AUTH_FAILURE & HTTP 401"]
        ConstantTimeCompare -->|Match| AuthSuccessAudit["Log AUTH_SUCCESS / Handle (/api/threads/* GDPR DELETE)"]
        Interactivity["Slack Interactivity Payload"] --> ActionSpec["ActionSpec Whole-Payload Pre-Auth"]
        ActionSpec -->|Unauthorized| DenyAction["Fail Closed (403) & Log ADMIN_ACTION_DENIED"]
        ActionSpec -->|Authorized| ChannelIDOR{"Scribe Download Channel Match?"}
        ChannelIDOR -->|Mismatch| DenyIDOR["HTTP 403 & Log UNAUTHORIZED_SCRIBE_DOWNLOAD"]
        ChannelIDOR -->|Match| AllowMutation["Execute Action / Atomic HITL Transition"]
    end

    subgraph Guardrails, Privileges & Tool Isolation
        UserPrompt["Human Prompt & Directives"] --> GuardrailIn["Guardrail Pre-Filter + Sliding-Window LanceDB Check"]
        GuardrailIn -->|Blocked| BlockInj["Block Turn & Log PROMPT_INJECTION_BLOCKED"]
        GuardrailIn -->|Pass| OriginTagger["Tag Directive Origin: 'slash' vs 'inferred'"]
        OriginTagger --> PrivBoundary{"Privileged Directive (/forget_thread, /grant_skill, etc.)?"}
        PrivBoundary -->|Yes| AdminCheck{"origin=='slash' AND is_slack_admin?"}
        AdminCheck -->|No| BlockPriv["Block & Log UNAUTHORIZED_DIRECTIVE_ATTEMPT"]
        AdminCheck -->|Yes| SafeExecute["Execute & Log ADMIN_DIRECTIVE_EXECUTED"]
        PrivBoundary -->|No| SafeExecute
        AttachedFile["Slack Attached Files"] --> ExtSanitizer["Neutralize Unsafe Extensions (.txt) & Wrap &lt;untrusted_file_content&gt;"]
        ToolOut["Tool / MCP Output"] --> IndirectScreen["screen_untrusted_tool_output (Heuristic / Opt-In Embedding)"]
        IndirectScreen --> AgentOutput["Agent Output"] --> Sanitizer["Neutralize Broadcast Mentions & Secret Scan"]
    end
```

---

## Slack Ingress: HMAC-SHA256 Signature Verification

Every incoming HTTP request to `/slack/events`, `/slack/actions`, or `/slack/interactivity` is inspected by [`core/slack_security.py`](file:///engine/core/slack_security.py).

### 1. Timestamp Freshness (Anti-Replay)
The header `X-Slack-Request-Timestamp` is compared against current system epoch time:
```python
if abs(time.time() - int(timestamp)) > 300:
    raise HTTPException(status_code=400, detail="Request timestamp out of bounds.")
```
Requests older than 5 minutes are discarded immediately to protect against recorded replay attacks.

### 2. Signature Computation & Constant-Time Comparison
The raw request body is concatenated into `v0:{timestamp}:{raw_body}`, signed with `SLACK_SIGNING_SECRET` via HMAC-SHA256, and verified using `hmac.compare_digest`.

---

## Administrative API & Interactivity Pre-Authorization

### 1. HTTP Basic Auth on `/api/*`
Administrative REST endpoints (`/api/*`) in [`engine/api/router.py`](file:///engine/api/router.py) and [`engine/api/dependencies.py`](file:///engine/api/dependencies.py) require HTTP Basic Authentication validated via `secrets.compare_digest`. If `ADMIN_PASSWORD` is unset, all `/api/*` requests fail closed with HTTP 500/401.

### 2. Whole-Payload `ActionSpec` Interactivity Gate
In [`adapters/slack/interactivity.py`](file:///engine/adapters/slack/interactivity.py), every Slack interactive `action_id` and modal `callback_id` is registered in `ACTION_REGISTRY` with an `ActionSpec(handler, admin_required=True)`. The dispatcher pre-authorizes **all** actions in an incoming payload before executing any handler, preventing partial execution of mixed payloads.

### 3. Atomic Single-Statement HITL State Transitions
Human-in-the-Loop approvals in [`services/hitl.py`](file:///engine/services/hitl.py) (`approve_request`, `deny_request`, `consume_approval`) use atomic single-statement SQLite `UPDATE ... WHERE request_id = ? AND status = ?` transitions with `cursor.rowcount == 1` verification. This eliminates check-then-act (TOCTOU) race conditions if two admins click Approve/Deny simultaneously, and binds approvals to a SHA-256 hash of the tool parameters (`params_hash`).

---

## Privilege Boundaries, File Neutralization & Prompt Safety

### 1. Declarative Admin Authorization SSOT & Dual Gate (`origin == "slash"` + `is_slack_admin`)
Administrative privilege requirements are defined directly in `SKILL.md` frontmatter via `requires_admin: true` as the Single Source of Truth (SSOT), and enforced across two architectural layers:
- **Fail-Closed Startup Abort**: `SkillRegistry` mandates `requires_admin: boolean` on every skill; missing or non-boolean properties raise `SkillIntegrityError` and abort boot.
- **Layer 1 (Directives)**: All directive actions in [`TurnContext`](file:///engine/orchestration/turn_pipeline.py) carry an explicit `origin` attribute (`"slash"` vs `"inferred"`). In [`orchestration/stages/directive_handlers.py`](file:///engine/orchestration/stages/directive_handlers.py), actions with `sk.requires_admin == True` (such as `/grant_skill`, `/grant_skill_for_all`, `/revoke_skill_for_all`, `/revoke_constraint`, `/forget_thread`, and disabling constraints via `=off`) require **both**:
  1. Explicit slash invocation (`origin == "slash"`).
  2. Slack Workspace Admin authorization (`is_slack_admin(ctx.input.user_id)`).
  Unauthorized attempts are blocked immediately at Stage 1 and emit an `UNAUTHORIZED_DIRECTIVE_ATTEMPT` audit event.
- **Layer 2 (Agent Tool RBAC)**: Agent-scoped skills with `requires_admin: true` are withheld from non-admin user turns during prompt building and tool execution unless explicitly granted for that thread by an admin, preventing unauthorized tool invocations and token spend.
- **Anti-IDOR Scribe Download Verification**: [`adapters/slack/actions/scribe_actions.py`](file:///engine/adapters/slack/actions/scribe_actions.py) verifies that the button target channel ID matches the incoming payload's originating channel/container context, returning HTTP `403` and logging `UNAUTHORIZED_SCRIBE_DOWNLOAD` on mismatch.

### 2. Slack Upload Extension Neutralization (`SAFE_DOWNLOAD_EXTENSIONS`)
When downloading files attached to Slack threads in [`services/slack/files.py`](file:///engine/services/slack/files.py):
- Only whitelisted document extensions (`.txt`, `.md`, `.pdf`, `.csv`, `.json`, `.yaml`, `.yml`, `.xml`, `.html`, `.log`) are stored with their original extension inside thread-scoped `data/downloads/<channel_id>_<thread_ts>/`.
- Any executable or other extension (e.g., `.py`, `.sh`, `.exe`, `.bat`, `.js`) is automatically neutralized by appending `.txt` (e.g., `exploit.py.txt`) and stored with restrictive `0o600` file permissions.
- Extracted file text is wrapped in `<untrusted_file_content>` tags before prompt injection.

### 3. PII Masking, Configurable NER Models & Automated Skill Exclusions
[`services/security/pii_masker.py`](file:///engine/services/security/pii_masker.py) provides non-blocking, worker-thread-offloaded `async_mask()` and single-pass `unmask()` using a unified alternation regex. Single-pass unmasking prevents recursive token-expansion attacks where one synthetic token's original value contains another synthetic token string:
- **Deterministic Regex Hardening**: Scrubs Stripe live/test/restricted keys (`sk_live_*`/`rk_*`), GitHub tokens (`ghp_*`, `github_pat_*`), quoted JSON secret values (`{"api_key": "..."}`), OpenAI/Anthropic/Groq/Google/AWS/JWT/Slack credentials, 15-digit Amex & 16-digit cards, SSNs, and boundary-guarded phone numbers (avoiding Slack timestamps/numeric IDs).
- **Configurable Multilingual Presidio NER, Acronym Guard & Process-Level Caching**: `SystemSettings.pii_ner_models` (default `["en_core_web_md"]`; changing it also requires updating the spaCy model wheel in [`pyproject.toml`](file:///pyproject.toml) / [`requirements.txt`](file:///requirements.txt) and installing the model) validates installed models via `spacy.util.is_package()` and logs warnings when unconfigured/uninstalled models fall back cleanly. `PERSON`/`LOCATION` NER guards skip 1-character spans and 2-letter ALL-CAPS acronyms (e.g., `JD`, `HR`) while keeping mixed-case short names and cities protected. Initialized `AnalyzerEngine` instances are cached process-wide per configured model tuple (`_PRESIDIO_ANALYZER_CACHE`), and `configure_ner_enabled()` allows deterministic regex-only operation when NER is scoped off.
- **Field-Scoped Word-Boundary Checkpoint Remasking**: Before persisting HITL or circuit-breaker checkpoints (`save_execution_checkpoint()`), `_remask_messages_for_checkpoint()` deep-copies messages and remasks only text content and `tool_calls[*].function.arguments` using word-boundary single-pass matching (preventing mid-word corruption), leaving opaque provider metadata (e.g., `thought_signature`, `thinking_blocks`, `signature`) untouched across all providers.
- **Per-Turn Outbound Masking Cache (`turn_masked_text_cache`)**: Repeated `execute_llm_with_retry()` invocations within the same turn (tool iterations and final synthesis) share [`TurnContext.turn_masked_text_cache`](file:///engine/orchestration/turn_pipeline.py) via [`_mask_text_with_cache()`](file:///engine/core/llm_utils.py) keyed by `(id(masker), ner_enabled, sha256(text))`, skipping redundant scans on unchanged history while bypassing the cache if an unmapped `<TYPE_N>` literal placeholder appears (preserving forged-placeholder guards).

#### A. Automated Skill Keyword Extraction & Hashing
When skills are registered or parsed from [`engine/skills/builtin/`](file:///engine/skills/builtin/) or [`config/skills/`](file:///config/skills/), [`SkillRegistry`](file:///engine/skills/registry.py) calculates a deterministic SHA-256 `content_hash` over skill metadata. Keyword extraction is strictly scoped to auto-detected skills (`auto_detect: true`) and agent skills (privileged slash-only skills are omitted to eliminate PII false negatives, since slash commands are already protected via `_PROTECTED_SPAN_PATTERNS`). During startup ([`pii_masker.warm_up()`](file:///engine/services/security/pii_masker.py)) and periodic background checks, [`extract_skill_exclusion_terms()`](file:///engine/services/security/pii_masker.py) extracts:
1. **Global Terms (`global_terms`)**: Exact skill names (`skill.name.lower()`), multi-word display names (`skill.display_name.lower()`), and uppercase acronyms ($\ge 2$ characters, e.g., `"RAG"`, `"PDF"`, `"MCP"`).
2. **Sentence-Start Keywords (`sentence_start_keywords`)**: Up to 5 lowercase tokens per skill (length $\ge 3$) extracted from bare slash command stems (e.g., `["max", "words"]` from `/max_words`), explicit `triggers.keywords`, and key terms from descriptions or extraction rules, filtered against stopwords and global allowlists.

#### B. Observable State Storage (`presidio_skill_exclusions.json`)
- **Canonical Path**: Excluded terms are persisted to [`data/state/presidio_skill_exclusions.json`](file:///data/state/presidio_skill_exclusions.json), resolved canonically via `PRESIDIO_SKILL_EXCLUSIONS_FILE` in [`core/paths.py`](file:///engine/core/paths.py).
- **Single Source of Truth**: `presidio_skill_exclusions.json` is the sole authority for skill keywords; `_STATIC_SENTENCE_START_ALLOWLIST_LOWER` contains only generic syntax tokens (`off`, `all`, `require`, `operations`).
- **Environment Override**: The storage path can be redirected using the `IZVOR_PRESIDIO_SKILL_EXCLUSIONS_FILE` environment variable.
- **Write-Protection**: Added to `DEFAULT_PROTECTED_WRITE_PATHS` in [`config_store/system_settings.py`](file:///engine/config_store/system_settings.py) to prevent unauthorized writes from tool agents.
- **Human Overrides**: Human administrators can define custom `manual_include` and `manual_exclude` lists per skill in `presidio_skill_exclusions.json`; these entries are preserved across automated re-syncs.

#### C. Context-Aware Sentence-Start Boundary Rule
Capitalized single-word skill keywords (e.g., `"Max"`, `"Min"`, `"Exclude"`, `"Include"`, `"Scribe"`) can easily collide with real people's names. To eliminate false-positive directive masking while preserving data privacy, [`PIIMasker.mask()`](file:///engine/services/security/pii_masker.py) employs [`_is_at_sentence_or_clause_start()`](file:///engine/services/security/pii_masker.py):
- **Clause/Sentence-Start Protection**: If a term is in `_sentence_start_allowlist` and appears at prompt start (ignoring leading Slack mentions `<@...>` or agent tags `/[...]`) or immediately after a clause boundary (`.`, `!`, `?`, `:`, `;`, `-`, `\n`), it is excluded from Presidio masking.
- **Mid-Sentence Privacy Enforcement**: If the same word appears mid-sentence without clause separation (e.g., *"Please ask Max about the budget in New York"*), Presidio's recognizer is permitted to mask the names as `<PERSON_*>`.

#### D. Periodic Synchronization & Low-Latency In-Memory Caching
- **Synchronization**: [`PIIMasker.sync_skill_exclusions()`](file:///engine/services/security/pii_masker.py) runs upon server boot and every 24 hours via an asynchronous background task `periodic_skill_exclusions_sync()` in [`main.py`](file:///main.py), governed by `skill_exclusions_sync_interval_seconds` (default: `86400.0`s). Disk writes occur only if skills were added, modified (hash mismatch), or removed.
- **Zero-Latency In-Memory Lookups**: Eliminating expensive dynamic registry traversals on the turn critical path, `PIIMasker` compiles precomputed `_global_allowlist: frozenset[str]` and `_sentence_start_allowlist: frozenset[str]`. Agent setting updates (e.g., adding/modifying agent display names or usernames) immediately invoke `pii_masker.refresh_allowlist()`.

### 4. LanceDB SQL Filter Validation (`_sql_literal`)
In [`services/rag/vector_store.py`](file:///engine/services/rag/vector_store.py), all metadata filter values passed to LanceDB `where` clauses reject NUL bytes / control characters and are validated against `_SAFE_ID_RE = ^[\w.:/ '-]{1,256}$` (Unicode-aware, doubling single quotes) in `_sql_literal()` to prevent filter-expression injection.

### 5. Slack Output Sanitization
Outgoing Slack messages are posted via [`services/slack/messages.py`](file:///engine/services/slack/messages.py) with `unfurl_links=False` and `unfurl_media=False`, and broadcast mentions (`<!channel>`, `<!here>`, `<!everyone>`) are neutralized. App Home Block Kit payloads pass through `sanitize_slack_blocks()` in [`services/slack/views.py`](file:///engine/services/slack/views.py).

---

## Path Traversal, Web SSRF & Sandbox Isolation

- **Fail-Closed `ToolPrincipal`**: [`SkillRegistry`](file:///engine/skills/registry.py) verifies [`ToolPrincipal`](file:///engine/core/domain.py) before executing any tool.
- **Filesystem Confinement**: `validate_resource_filename` in [`services/local_tools.py`](file:///engine/services/local_tools.py) confines operations to `agents_resources/`, supports safe Unicode filenames while rejecting control characters and trailing dots/spaces, filters symlink escapes in `list_documents`, truncates oversized reads at `MAX_READ_DOCUMENT_CHARS = 50000`, blocks Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`) and NTFS ADS (`:`), protects `DEFAULT_PROTECTED_WRITE_PATHS`, writes atomically via temp file + `os.replace()`, and caps writes at `max_write_bytes` (1 MB).
- **Outbound Web SSRF & Slowloris Protection**: [`services/web_tools.py`](file:///engine/services/web_tools.py) uses `PinnedIPTransport` to pin resolved public IPs, enforces `allowed_ports` (`[80, 443]`, including implicit default ports on `http`/`https`), fails closed on missing/empty `Content-Type`, bounds streamed reads by a monotonic wall-clock deadline, blocks private IPv4/IPv6/NAT64 ranges, optionally strips query strings and fragments on untrusted domains when `strip_query_on_untrusted_domains: true` (default: `false`), protects `ddgs` initialization via `_DDGS_ENGINE_LOCK`, and enforces a per-turn web fetch cap (`max_fetches_per_turn: 5`).

---

## Log Credential Sanitization & Data Protection

[`core/logging_filters.py`](file:///engine/core/logging_filters.py) invokes `setup_credential_redaction_filters()` at startup to attach `CredentialRedactionFilter` to Python's root logger **plus** `uvicorn`, `uvicorn.error`, `uvicorn.access`, and `httpx` loggers/handlers (since several third-party loggers set `propagate=False`). Before emitting records, the filter formats `%s` `record.args` strings prior to redaction and scrubs `Authorization: Bearer` & `Authorization: Basic` headers, Slack tokens (`xox*-*`), OpenAI/Anthropic/Gemini/Mistral/Groq/AWS keys, and private keys.

---

## Semantic Prompt Injection, Indirect Tool Screening & Jailbreak Guardrail

[`services/security/guardrail.py`](file:///engine/services/security/guardrail.py) protects both inbound user prompts and untrusted tool outputs:
1. **Zero-Cost Heuristic Pre-Filter (`check_heuristic_injection`)**: Screens inbound prompts immediately against regex rules (`instruction_override`, `role_jailbreak`, `prompt_exfiltration`, `delimiter_tag_injection`), emitting `PROMPT_INJECTION_BLOCKED` at zero embedding API cost.
2. **Capped Sliding-Window Embeddings & LRU Cache**: Splits long inputs into overlapping windows (`guardrail_chunk_chars: 800`, `guardrail_chunk_overlap_chars: 200`, capped at `guardrail_max_chunks: 8` with deterministic head/middle/tail sampling), evaluates single-batch embeddings cached in a 500-entry LRU cache (`guardrail_embedding_cache_size`), and searches LanceDB `guardrail_signatures` (threshold `0.85`).
3. **Indirect Tool-Output Injection Screening (`screen_untrusted_tool_output`)**: Invoked in [`ToolLoopRunner`](file:///engine/orchestration/stages/tool_loop_runner.py) on tool outputs (`fetch_web_page`, `web_search`, `read_document`, MCP tool responses), running heuristic checks always and escalating to LanceDB embeddings when `guardrail_indirect_embedding_enabled: true`. Detected payloads are neutralized and logged as `INDIRECT_INJECTION_DETECTED`.
4. **Configurable Fail-Mode (`guardrail_fail_mode`)**:
   - `"open"` (Default): Logs a `GUARDRAIL_DEGRADED_OPEN` warning and allows the turn if the vector store or embedding provider is unreachable.
   - `"closed"`: Fails closed, blocks the turn, posts a user-visible notice to Slack, and logs a critical `GUARDRAIL_FAIL_CLOSED_BLOCKED` audit event.

---

## Denial of Service (DoS) Defense & Audit Rate-Limiting

Managed in [`core/slack_security.py`](file:///engine/core/slack_security.py) and configured via `security_burst_limits` in `system_settings.json`:
- **Sliding-Window Throttling**: Monitors `AUTH_FAILURE` events over a `60.0`-second window (`auth_failure_window_seconds`).
- **Thresholds**: Up to `5` discrete failure events per client IP (`auth_failure_burst_limit`) and `25` globally (`global_auth_failure_burst_limit`), with tracker memory bounded at `1000` entries (`max_tracker_entries`). Excess failures are aggregated into a single summary event.
