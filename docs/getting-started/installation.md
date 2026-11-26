# Installation & Configuration Guide

This guide covers installing, configuring, and running **Izvor** locally or in production environments.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Step-by-Step Installation](#step-by-step-installation)
  - [1. Clone the Repository](#1-clone-the-repository)
  - [2. Set Up a Virtual Environment](#2-set-up-a-virtual-environment)
  - [3. Install Dependencies](#3-install-dependencies)
- [Environment Configuration](#environment-configuration)
  - [Configuration Variables Breakdown](#configuration-variables-breakdown)
- [Configuration vs. Engine Architecture Guide](#configuration-vs-engine-architecture-guide)
  - [1. Implementer Configuration (`config/`) — *Customize Freely*](#1-implementer-configuration-config--customize-freely)
  - [2. Core Engine & Built-ins (`engine/`) — *Do Not Modify During Configuration*](#2-core-engine--built-ins-engine--do-not-modify-during-configuration)
  - [3. Runtime & Slack UI State (`data/`) — *Managed Automatically at Runtime*](#3-runtime--slack-ui-state-data--managed-automatically-at-runtime)
  - [System Settings (`config/settings/system_settings.json`)](#system-settings-configsettingssystem_settingsjson)
- [Running the Application](#running-the-application)
  - [Development Mode](#development-mode)
  - [Windows Proactor Loop Notice](#windows-proactor-loop-notice)
  - [Health Check Verification](#health-check-verification)
- [Public Ingress & Cloudflare Tunnel Setup](#public-ingress--cloudflare-tunnel-setup)
  - [Option A: Cloudflare Tunnel — Persistent Named Domain (Recommended)](#option-a-cloudflare-tunnel--persistent-named-domain-recommended)
  - [Option B: Cloudflare Quick Tunnel (Zero-Account Local Testing)](#option-b-cloudflare-quick-tunnel-zero-account-local-testing)
  - [Option C: `ngrok`](#option-c-ngrok)

---

## Prerequisites

- **Python**: Version 3.11 or newer (Python 3.14+ fully verified and supported).
- **Package Manager**: `pip` (or `uv` / `poetry`).
- **Slack Workspace**: Admin or Developer privileges to create and install a Slack App.
- **LLM API Access**: An API key for at least one supported provider (e.g., Google Gemini, Mistral, Anthropic, OpenAI, or OpenRouter).

---

## Step-by-Step Installation

### 1. Clone the Repository

```bash
git clone https://github.com/BranMik/Izvor.git izvor
cd izvor
```

### 2. Set Up a Virtual Environment

On Linux / macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies

For production or runtime execution (`requirements.txt` or reproducible `uv.lock`):
```bash
pip install --upgrade pip
pip install -r requirements.txt
# Or with uv (reproducible lockfile):
uv sync --frozen --no-dev
```

For local development and running the test suite (managed via [`pyproject.toml`](file:///pyproject.toml), [`uv.lock`](file:///uv.lock), and [`requirements-dev.txt`](file:///requirements-dev.txt)):
```bash
pip install -r requirements-dev.txt
# Or with uv:
uv sync --frozen --group dev
```

Izvor utilizes:
- `fastapi`, `uvicorn`, and `python-multipart` for the asynchronous ASGI server and Slack form-encoded interactivity parsing.
- `slack-sdk` for Slack WebClient operations and Block Kit rendering.
- `litellm` for universal multi-provider LLM completions and embeddings.
- `lancedb`, `tantivy`, `pyarrow`, and `pandas` for serverless hybrid vector + BM25 RAG retrieval and prompt-injection guardrails.
- `presidio-analyzer` and `spacy` (`en_core_web_sm` by default; configurable via `pii_ner_models`) for local NER + regex two-way PII masking.
- `mcp` for external Model Context Protocol (`stdio` / `sse` / `streamable_http`) client integration.
- `httpx`, `httpcore`, `ddgs`, `beautifulsoup4`, and `pypdf` for SSRF-guarded web research and document parsing.
- `pydantic` and `pyyaml` for domain models, configuration validation, and `SKILL.md` frontmatter parsing.
- Standard-library `sqlite3` (`WAL` mode) for zero-infrastructure ACID state persistence, plus `ruff`, `mypy`, `pytest-xdist`, and `pip-audit` (`[dependency-groups].dev` in [`pyproject.toml`](file:///pyproject.toml) and [`uv.lock`](file:///uv.lock)) for linting, strict core/protocol type checking, parallel tests, and dependency vulnerability auditing.

---

## Environment Configuration

Create a `.env` file in the project root by copying [`.env.example`](file:///.env.example):

### Configuration Variables Breakdown
Refer to [`core/config.py`](file:///engine/core/config.py) and [`core/paths.py`](file:///engine/core/paths.py) for the full definitions:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | **Yes** | `""` | Bot user OAuth token (`xoxb-...`) with write/read scopes. |
| `SLACK_SIGNING_SECRET` | **Yes** | `""` | Slack signing secret for validating HMAC-SHA256 signatures. |
| `SLACK_ADMIN_USER_IDS` | Recommended | `""` | Comma-delimited list of Slack User IDs authorized for App Home admin actions and privileged directives. |
| `GEMINI_API_KEY` | Optional | `None` | API key for Google Gemini completions and RAG embeddings (`gemini/gemini-embedding-001`). |
| `MISTRAL_API_KEY` | Optional | `None` | API key for Mistral direct access. |
| `ANTHROPIC_API_KEY` | Optional | `None` | API key for Anthropic Claude direct access. |
| `OPENAI_API_KEY` | Optional | `None` | API key for OpenAI direct access. |
| `OPENROUTER_API_KEY` | Optional | `None` | API key for OpenRouter (plus optional `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `COHERE_API_KEY`, `PERPLEXITY_API_KEY`, `TOGETHER_API_KEY`, `META_API_KEY`, `META_API_BASE`). |
| `ADMIN_USERNAME` | No | `admin` | HTTP Basic Auth username for protected `/api/*` endpoints. |
| `ADMIN_PASSWORD` | **Yes** (for API) | `""` | HTTP Basic Auth password for protected `/api/*` endpoints. |
| `AUDIT_HMAC_SECRET` | **Yes** (in Prod unless keyring set) | `""` | Single secret key (`>= 32` chars) for signing non-repudiable HMAC-chained audit logs (fail-fast required when `ENVIRONMENT=production` unless `AUDIT_HMAC_KEYS_JSON` is provided). |
| `AUDIT_HMAC_KEYS_JSON` | Optional | `""` | JSON keyring mapping `{key_id: secret}` for zero-downtime HMAC key rotation across historical audit files. |
| `AUDIT_HMAC_ACTIVE_KEY_ID` | Optional | `""` | Active key identifier in `AUDIT_HMAC_KEYS_JSON` used for newly written audit records (falls back to `SystemSettings.audit_hmac_key_id`). |
| `PORT` | No | `8000` | HTTP port on which FastAPI listens. |
| `BASE_AVATAR_URL` | Recommended | `https://izvor.local/` | Public HTTPS URL of this deployment so Slack servers can fetch custom persona avatars at `/avatars/*`. |
| `TOOLS_WORKSPACE_DIR` | No | `.` | Workspace root directory (`IZVOR_WORKSPACE_DIR` also supported); agent document tools remain sandboxed to `agents_resources/`. |
| `IZVOR_DB_PATH` | No | `data/izvor.db` | Path to persistent SQLite WAL state database (part of the `IZVOR_*` path overrides in [`core/paths.py`](file:///engine/core/paths.py)). |
| `WEB_CONCURRENCY` | No | `1` | Must remain `1`; Izvor coordinates in-memory `asyncio.Task` cancellation and thread state within a single worker process. |
| `THREAD_STATE_TTL_SECONDS` | No | `3600.0` | Idle TTL in seconds before inactive in-memory thread state is evicted (`MAX_TRACKED_THREADS` defaults to `500`). |
| `USAGE_CHANNEL_NAME` | No | `izvor-usage` | Default Slack channel name (`USAGE_CHANNEL_ID` optional) for posting thread token/cost summaries. |

> [!WARNING]
> If `ADMIN_PASSWORD` is left unset, all protected endpoints (`/api/*`) will reject incoming requests with HTTP 500/401 to prevent unauthenticated access. Always set a strong, random password.

---

## Configuration vs. Engine Architecture Guide

Izvor separates its codebase into three distinct physical pillars ([`core/paths.py`](file:///engine/core/paths.py)) so implementers know exactly what to customize and what to leave untouched:

### 1. Implementer Configuration (`config/`) — *Customize Freely*
All domain-specific customization lives inside `config/`:
- **Debate Panel Roster ([`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json))**: Define your debate panel agents (16 default OOB: `agent-10001`..`agent-10016`), their display names, LLM models, `is_short_output` flags, assigned tool skills, `enabled_agents`, and active `workflow_config`.
- **Debate Persona Prompts ([`config/personas/`](file:///config/personas))**: Edit or add markdown system prompts (e.g., `config/personas/agent-10001.md`) to shape each agent's personality, domain expertise, and analytical style.
- **Workflow Topologies ([`config/workflows/`](file:///config/workflows))**: Create or modify custom multi-agent workflow JSON presets (Sandwich chains, Fixed sequences, Parallel Dialectic pools).
- **Agent Tool Skills ([`config/skills/`](file:///config/skills))**: Add or customize extensible tool-backed agent skills (4 OOB: `web_research`, `knowledge_base_rag`, `document_reader`, `document_authoring`).
- **System Operational Settings ([`config/settings/system_settings.json`](file:///config/settings/system_settings.json))**: Tune global model defaults, RAG parameters, timeouts, MCP servers, and rate limits.
- **Avatars & RAG Knowledge ([`config/avatars/`](file:///config/avatars), [`config/rag_knowledge/`](file:///config/rag_knowledge))**: Drop custom avatar PNGs into `config/avatars/` and domain markdown/text/PDF documents into `config/rag_knowledge/shared/` or `config/rag_knowledge/<agent_id>/`.

### 2. Core Engine & Built-ins (`engine/`) — *Do Not Modify During Configuration*
The `engine/` directory contains the core Python runtime (**152 modules** across **11 packages**, including `engine/config_store/` and `engine/api/`) and immutable system definitions. Avoid modifying files in `engine/` unless performing a deliberate architectural refactoring:
- **System Agents & Orchestrator ([`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json))**: Immutable baseline configuration for the `orchestrator` (`fast_model`, `reasoning_model`) and the **4 built-in `system_agents`** (`InternalRevisor`, `Challenger`, `Arbitrator`, and `DiscussionScribe`). Runtime overrides modified via Slack App Home are written to `data/settings/system_agent_settings.json`.
- **Built-in System Prompts ([`engine/prompts/system_agents/`](file:///engine/prompts/system_agents) & [`engine/prompts/shared/`](file:///engine/prompts/shared))**: Core prompt assembly rules, nomination syntax, and system prompt templates (`Arbitrator.md`, `Challenger.md`, `Discussion_Scribe_Live.md`, `Discussion_Scribe_Conclusion.md`, `InternalRevisor.md`, `Intent_Extractor.md`, `Language_Detector.md`, `Pricing_Agent.md`).
- **Built-in Control & Modifier Skills ([`engine/skills/builtin/`](file:///engine/skills/builtin))**: **18 built-in** orchestration and directive skills (`control_plane`, `first`, `forget_thread`, `rounds`, `max_words`, `min_words`, `scribe_max_words`, `scribe_min_words`, `open_debate`, `grant_skill`, `grant_skill_for_all`, `revoke_skill_for_all`, `revoke_constraint`, `exclude_agents`, `include_agents`, `list_skills`, `stop_discussion`, `thread_history`).
- **Seed Workflows ([`engine/workflows/`](file:///engine/workflows))**: Protected system default workflow presets (`default_review_flow.json`, `default_sandwich.json`, `parallel_dialectic_flow.json`).

### 3. Runtime & Slack UI State (`data/`) — *Managed Automatically at Runtime*
The `data/` directory stores live databases, caches, audit logs, and Slack UI state:
- **Slack App Home State ([`data/state/app_settings.json`](file:///data/state/app_settings.json))**: Stores runtime toggles (circuit breaker thresholds, daily USD budget, Scribe toggle, convergence fast-forward, workspace baseline context). **Modify these via the Slack App Home UI rather than editing the JSON file directly.**
- **Presidio Skill Exclusions ([`data/state/presidio_skill_exclusions.json`](file:///data/state/presidio_skill_exclusions.json))**: Automatically synchronized keyword allowlists for registered skills so Presidio NER never masks valid skill or tool identifiers.
- **SQLite State Engine (`data/izvor.db`)**, **Audit Logs (`data/audit/`)**, **LanceDB Indices (`data/.rag_cache/lancedb/`)**, **Pricing Cache (`data/pricing/`)**, **Backups (`data/backups/`)**, **Scribe Thread Notes (`data/thread_notes/`)**, **Thread Downloads (`data/downloads/`)**, and **Transcripts (`data/flat-histories/`)**.

---

### System Settings (`config/settings/system_settings.json`)

System operational parameters, security guardrails, and timeouts are managed by [`SystemSettingsManager`](file:///engine/config_store/system_settings.py) (backed by [`JsonSettingsStore`](file:///engine/config_store/settings_store.py)):

| Setting Key | Shipped Value (`system_settings.json`) | Description |
|---|---|---|
| `default_fast_model` | `"gemini/gemini-3.8-flash"` | System-level fallback model for fast routing/parsing tasks. |
| `default_reasoning_model` | `"mistral/mistral-large-latest"` | System-level fallback model for deep deliberation and Stage 2 revisor confirmation. |
| `log_level` | `"INFO"` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `strict_privacy_mode_enabled` | `true` | Enforces two-way Presidio + regex PII masking on outbound LLM prompts. |
| `pii_ner_models` | `["en_core_web_sm"]` *(schema default)* | Configurable list of installed spaCy NER models validated via `spacy.util.is_package()` for multilingual Presidio detection. |
| `internal_revisor_enabled` | `true` | Enables Two-Stage `InternalRevisor` post-turn hallucination/contradiction verification. |
| `shared_evidence_max_chars` | `40000` | Maximum character budget for `<shared_round_evidence>` and `<retrieved_evidence>` blocks. |
| `debate_turn_pacing_seconds` | `3.0` | Delay between consecutive agent turns to prevent flooding. |
| `tool_call_loop_timeout_seconds` | `60.0` | Maximum wall-clock execution time for iterative tool calling loops and bounded final tool-free synthesis. |
| `slack_post_pause_seconds` | `2.0` | Brief pause before posting to Slack to allow human visual parsing. |
| `max_file_size_mb` | `50.0` | Maximum allowable file size (MB) for uploads and workspace document reading. |
| `max_write_bytes` | `1048576` | Hard ceiling in bytes (1 MB) for individual `write_document` workspace writes. |
| `guardrail_fail_mode` | `"open"` | Prompt-injection guardrail error fallback mode (`"open"` allows turn with warning, `"closed"` blocks execution and logs `GUARDRAIL_FAIL_CLOSED_BLOCKED`). |
| `guardrail_embedding_cache_size` | `500` | LRU cache capacity for prompt embedding similarity calculations. |
| `guardrail_chunk_chars` | `800` *(schema default)* | Maximum characters per sliding window during semantic prompt-injection checks. |
| `guardrail_chunk_overlap_chars` | `200` *(schema default)* | Overlap between adjacent prompt-injection sliding windows. |
| `guardrail_max_chunks` | `8` *(schema default)* | Maximum sampled sliding windows embedded per prompt (`head`/`middle`/`tail` sampling). |
| `guardrail_indirect_embedding_enabled` | `false` *(schema default)* | Opt-in embedding similarity check on untrusted tool outputs (`screen_untrusted_tool_output` always runs zero-cost heuristics). |
| `audit_hmac_key_id` | `"default"` *(schema default)* | Fallback active HMAC `key_id` when `AUDIT_HMAC_ACTIVE_KEY_ID` is unset. |
| `max_discussion_turns` | `20` | Hard ceiling for total agent turns in a single thread deliberation. |
| `max_tool_iterations` | `3` | Maximum tool execution iterations per single agent turn. |
| `tool_call_num_retries` | `2` | Number of retries permitted for transient tool invocation failures. |
| `word_limit_grace_buffer` | `50` | Word count buffer above `max_words` before mechanical truncation is applied (`0` when `max_words < 20`). |
| `output_constraints` | `{"default_max_words": 150, "short_max_words": 70}` | Configured prompt word ceilings (clamped `1..2000`; schema fallback `200`/`100`) appended via `output_constraints.md` for standard and short-output personas; limits `< 60` switch prompts to compact mode. |
| `system_agent_inference_timeout_seconds` | `20.0` | Wall-clock timeout in seconds for system agent inference (`InternalRevisor`, `DiscussionScribe`, `IntentExtractor`). |
| `system_agent_inference_retry_timeout_multiplier` | `1.5` | Timeout multiplier applied on system agent retry attempts. |
| `thread_cleanup_interval_seconds` | `600.0` | Frequency in seconds for pruning idle in-memory thread states (10 minutes). |
| `audit_integrity_check_interval_seconds` | `43200.0` | Frequency in seconds for verifying the cryptographic hash chain of audit logs (12 hours). |
| `skill_exclusions_sync_interval_seconds` | `86400.0` *(schema default)* | Frequency in seconds for periodic synchronization of Presidio skill keyword exclusions (24 hours, default in [`SystemSettingsModel`](file:///engine/config_store/system_settings.py)). |
| `pricing_cache_ttl_days` | `7` | Cache lifespan in days for LLM token pricing benchmarks. |
| `slack_file_download_timeout_seconds` | `30.0` | Timeout in seconds for downloading attached files from Slack. |
| `web_search_timeout_seconds` | `10.0` | Timeout in seconds for outbound `ddgs` web searches. |
| `web_fetch_timeout_seconds` | `12.0` | Timeout in seconds for fetching web pages via DNS-pinned HTTPX scraper. |
| `db_busy_timeout_seconds` | `5.0` | SQLite lock wait timeout in seconds (5,000ms) before raising database busy error. |
| `llm_max_retries` | `3` | Max automatic retries for transient LiteLLM provider errors. |
| `llm_retry_backoff_base` | `1.5` | Exponential backoff multiplier base for LLM retries. |
| `slack_rate_limit_per_second` | `10.0` | Outbound Slack API token bucket refill rate per second. |
| `slack_rate_limit_burst` | `15.0` | Maximum burst capacity for Slack API token bucket. |
| `pricing_benchmark_url` | `"https://artificialanalysis.ai/models"` | Endpoint for remote LLM token pricing benchmarks. |
| `web_fetch_user_agent` | Chrome/Mozilla UA string | HTTP `User-Agent` header for outbound web fetching. |
| `web_security` | `{...}` | Outbound web safety rules (`max_url_length: 2048`, `max_query_length: 1024`, `max_redirects: 5`, `max_body_bytes: 2097152`, `allowed_ports: [80, 443]`, `allowed_domains: []`, `strip_query_on_untrusted_domains: false`, `max_fetches_per_turn: 5`). |
| `security_burst_limits` | `{...}` | Sliding-window rate limiters for failed authentication (`auth_failure_window_seconds: 60.0`, `auth_failure_burst_limit: 5`, `global_auth_failure_burst_limit: 25`, `max_tracker_entries: 1000`). |
| `dialectical_stress_test` | `{"enabled": true, "consecutive_threshold": 3}` | Dialectical stress-test parameters governing when consecutive `signal_consensus_convergence` turns trigger the `Challenger` → `Arbitrator` gate. |
| `retention_checkpoints_days` | `7` | Retention window in days before expired `turn_checkpoints` rows are deleted. |
| `retention_traces_days` | `30` | Retention window in days before `turn_traces` telemetry spans are deleted. |
| `retention_event_dedup_days` | `1` | Retention window in days before `event_dedup` rows are purged. |
| `retention_expired_grace_days` | `7` | Grace window in days past expiration before `hitl_approvals` and `thread_skills` rows are deleted. |
| `retention_thread_sessions_days` | `90` | Retention window in days before inactive `thread_sessions` (and linked context/usage/notes rows) are purged (`0` disables). |
| `retention_spend_ledger_days` | `0` | Opt-in retention window in days before `spend_ledger` rows are deleted (`0` = disabled by default). |
| `retention_downloads_hours` | `24` | TTL in hours before cached Slack attachments in `data/downloads/` are evicted (`0` disables). |
| `retention_flat_histories_days` | `0` | Opt-in retention window in days before `data/flat-histories/` and `data/thread_notes/` Markdown archives are deleted (`0` = disabled by default). |
| `retention_audit_anchors_compact_enabled` | `true` | Compacts intermediate `audit_anchors` rows on closed past months (always keeping terminal `MAX(seq)` for on-disk `audit_YYYY_MM.jsonl` files) and prunes orphaned months. |
| `retention_prune_interval_seconds` | `86400.0` | Background interval in seconds between scheduled `prune_expired_records()` runs (24 hours). |
| `workspace_write_blacklist_additional` | `[]` | Additional filename patterns blocked from workspace `write_document` operations. |
| `rag` | `{...}` | Chunking and LanceDB embedding settings (`max_chunk_chars: 1500`, `chunk_overlap_chars: 300`, `min_chunk_chars: 80`, `default_top_k: 3`, `embedding_model: "gemini/gemini-embedding-001"`, `embedding_dim: 3072`, `embedding_batch_size: 32`, `pii_masking: "AT_RETRIEVAL"`, `guardrail_threshold: 0.85`). See [configuration-guide.md](configuration-guide.md#63-tuning-rag--security-parameters-configsettingssystem_settingsjson) for per-key details. |
| `mcp_servers` | `{}` | External MCP server configurations (`stdio`, `sse`, `streamable_http` transports). See [configuration-guide.md](configuration-guide.md#41-mcp_servers-configuration-reference-configsettingssystem_settingsjson) for all server fields. |

#### `web_security` Sub-Keys

| Key | Shipped Value | Description |
|---|---|---|
| `max_url_length` | `2048` | Maximum permitted character length of a URL passed to `fetch_web_page`. |
| `max_query_length` | `1024` | Maximum permitted character length of a search query passed to `web_search`. |
| `max_redirects` | `5` | Maximum HTTP redirect hops followed by `fetch_web_page` (each hop is independently SSRF-validated). |
| `max_body_bytes` | `2097152` | Hard cap in bytes (2 MB) on streamed HTTP response bodies downloaded by `fetch_web_page`. |
| `allowed_ports` | `[80, 443]` | Permitted destination TCP ports (including implicit default ports for `http`/`https`). |
| `allowed_domains` | `[]` | Optional domain allowlist (case-insensitive exact host or subdomain suffix match). Empty `[]` allows all public domains subject to SSRF IP filtering. |
| `strip_query_on_untrusted_domains` | `false` | When `true`, strips URL query strings and fragments from `fetch_web_page` requests targeting hosts outside `allowed_domains` (or all hosts when `allowed_domains` is empty). |
| `max_fetches_per_turn` | `5` | Per-turn cap on outbound web requests enforced via `contextvars.ContextVar`. |

#### `security_burst_limits` Sub-Keys

| Key | Shipped Value | Description |
|---|---|---|
| `auth_failure_window_seconds` | `60.0` | Sliding time window in seconds for counting failed authentication attempts (`AUTH_FAILURE`). |
| `auth_failure_burst_limit` | `5` | Maximum discrete `AUTH_FAILURE` audit events logged per client IP within the sliding window before excess events are aggregated. |
| `global_auth_failure_burst_limit` | `25` | Maximum discrete `AUTH_FAILURE` audit events logged globally across all IPs within the sliding window before aggregation. |
| `max_tracker_entries` | `1000` | Upper bound on tracked client IP entries in the in-memory burst tracker to prevent memory exhaustion. |

#### `dialectical_stress_test` Sub-Keys

| Key | Shipped Value | Description |
|---|---|---|
| `enabled` | `true` | Enables or disables the mandatory `Challenger` → `Arbitrator` dialectical stress-test gate before convergence fast-forwarding. When `false`, convergence fast-forward skips the stress-test gate. |
| `consecutive_threshold` | `3` | Consecutive turns emitting `signal_consensus_convergence` required to trigger the `Challenger` → `Arbitrator` gate (clamped `>= 1`). |

---

## Running the Application

### Development Mode

Run the server with Uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Windows Proactor Loop Notice

On Windows platforms, Python's Proactor event loop can occasionally stall when no incoming HTTP sockets are active. Izvor automatically starts a background heartbeat coroutine [`keep_loop_alive`](file:///main.py#L73-L79) to keep the loop responsive without measurable CPU overhead.

### Health Check Verification

Once running, verify service availability:

```bash
curl http://localhost:8000/health
```

Expected JSON response:
```json
{
  "status": "healthy",
  "service": "Izvor Multi-Agent Hub"
}
```

To view the active agent catalog, use the authenticated administrative endpoint:
```bash
curl -u admin:YourAdminPassword http://localhost:8000/api/health
```

---

## Public Ingress & Cloudflare Tunnel Setup

Because Slack delivers event notifications (`/slack/events`), interactive component payloads (`/slack/interactivity`), and server-side avatar fetches (`/avatars/*`) via HTTPS to a public URL, your Izvor server must be reachable from the internet **before** you verify your Slack App webhook URLs.

### Option A: Cloudflare Tunnel — Persistent Named Domain (Recommended)
If you manage a domain on Cloudflare, a named tunnel gives you a stable public hostname (so you do not have to update your Slack App URLs on every restart):

1. **Install `cloudflared`** ([Cloudflare Tunnel downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)) and authenticate:
   ```bash
   cloudflared tunnel login
   ```
2. **Create a named tunnel** and route a DNS hostname to it:
   ```bash
   cloudflared tunnel create izvor-hub
   cloudflared tunnel route dns izvor-hub izvor.yourdomain.com
   ```
3. **Configure `~/.cloudflared/config.yml`**:
   ```yaml
   tunnel: <YOUR-TUNNEL-UUID>
   credentials-file: /path/to/.cloudflared/<YOUR-TUNNEL-UUID>.json

   ingress:
     - hostname: izvor.yourdomain.com
       service: http://localhost:8000
     - service: http_status:404
   ```
4. **Run the tunnel**:
   ```bash
   cloudflared tunnel run izvor-hub
   ```

### Option B: Cloudflare Quick Tunnel (Zero-Account Local Testing)
For instant local testing without a custom domain, start an ephemeral TryCloudflare tunnel:
```bash
cloudflared tunnel --url http://localhost:8000
```
Copy the generated `https://<random-words>.trycloudflare.com` URL.

### Option C: `ngrok`
```bash
ngrok http 8000
```

> [!IMPORTANT]
> Once your tunnel is running, set `BASE_AVATAR_URL=https://<your-tunnel-hostname>/` in your `.env` file (Slack fetches agent avatar icons server-side from this URL) and configure your webhook URLs (`https://<your-tunnel-hostname>/slack/events` and `https://<your-tunnel-hostname>/slack/interactivity`) following the [Slack App Setup Guide](file:///docs/getting-started/slack-app-setup.md).
