# Installation & Configuration Guide

This guide covers installing, configuring, and running **Izvor** locally or in production environments.

---

## 📋 Prerequisites

- **Python**: Version 3.11 or newer (Python 3.14+ fully verified and supported).
- **Package Manager**: `pip` (or `uv` / `poetry`).
- **Slack Workspace**: Admin or Developer privileges to create and install a Slack App.
- **LLM API Access**: An API key for at least one supported provider (e.g., OpenRouter, OpenAI, Anthropic, Google Gemini, or Mistral).

---

## ⚙️ Step-by-Step Installation

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

For production or runtime execution:
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

For local development and running the test suite (managed via [`pyproject.toml`](file:///pyproject.toml) and [`requirements-dev.txt`](file:///requirements-dev.txt)):
```bash
pip install -r requirements-dev.txt
```

Izvor utilizes:
- `fastapi` and `uvicorn` for the high-performance async server.
- `slack-sdk` for Slack WebClient operations and Block Kit rendering.
- `litellm` for universal multi-provider LLM routing.
- `httpx` for asynchronous HTTP calls and MCP client adapters.
- `pydantic` for data validation.
- Standard library `sqlite3` for zero-infrastructure WAL state persistence.

---

## 🔐 Environment Configuration

Create a `.env` file in the project root by copying [`.env.example`](file:///.env.example):

### Configuration Variables Breakdown
Refer to [`core/config.py`](file:///engine/core/config.py) for the full definitions:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | **Yes** | `None` | Bot user OAuth token (`xoxb-...`) with write/read scopes. |
| `SLACK_SIGNING_SECRET` | **Yes** | `None` | Slack signing secret for validating HMAC-SHA256 signatures. |
| `SLACK_ADMIN_USER_IDS` | Recommended | `""` | Comma-delimited list of Slack User IDs authorized for App Home admin actions. |
| `OPENROUTER_API_KEY` | Optional | `None` | API key for OpenRouter (recommended universal router). |
| `OPENAI_API_KEY` | Optional | `None` | API key for OpenAI direct access. |
| `ANTHROPIC_API_KEY` | Optional | `None` | API key for Anthropic direct access. |
| `GEMINI_API_KEY` | Optional | `None` | API key for Google Gemini direct access. |
| `MISTRAL_API_KEY` | Optional | `None` | API key for Mistral direct access. |
| `ADMIN_USERNAME` | No | `admin` | HTTP Basic Auth username for protected `/api/*` endpoints. |
| `ADMIN_PASSWORD` | **Yes** (for API) | `None` | HTTP Basic Auth password for protected endpoints. |
| `AUDIT_HMAC_SECRET` | Optional | `None` | Secret key used for signing non-repudiable audit logs. |
| `PORT` | No | `8000` | HTTP port on which FastAPI listens. |
| `IZVOR_DB_PATH` | No | `data/izvor.db` | Path to persistent SQLite state database. |
| `MCP_WORKSPACE_DIR` | No | `.` | Root directory to which MCP filesystem tools are confined. |

> [!WARNING]
> If `ADMIN_PASSWORD` is left unset, all protected endpoints (`/api/*`) will reject incoming requests with HTTP 500/401 to prevent unauthenticated access. Always set a strong, random password.

---

## 🏗️ Configuration vs. Engine Architecture Guide

Izvor separates its codebase into three distinct directories so implementers know exactly what to customize and what to leave untouched:

### 1. Implementer Configuration (`config/`) — *Customize Freely*
All domain-specific customization lives inside `config/`:
- **Debate Panel Roster ([`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json))**: Define your debate panel agents (`agent-10001`..`agent-10016`), their display names, LLM models, assigned tool skills, `enabled_agents`, and active `workflow_config`.
- **Debate Persona Prompts ([`config/personas/`](file:///config/personas))**: Edit or add markdown system prompts (e.g., `config/personas/agent-10001.md`) to shape each agent's personality, domain expertise, and analytical style.
- **Workflow Topologies ([`config/workflows/`](file:///config/workflows))**: Create or modify multi-agent workflow JSON presets (Sandwich chains, Fixed sequences, Parallel Dialectic pools).
- **Agent Tool Skills ([`config/skills/`](file:///config/skills))**: Add or customize tool-backed agent skills (`web_research`, `knowledge_base_rag`, `document_reader`, `document_authoring`).
- **System Operational Settings ([`config/settings/system_settings.json`](file:///config/settings/system_settings.json))**: Tune global model defaults, RAG parameters, timeouts, and rate limits.
- **Avatars & RAG Knowledge ([`config/avatars/`](file:///config/avatars), [`config/rag_knowledge/`](file:///config/rag_knowledge))**: Drop custom avatar PNGs into `config/avatars/` and domain markdown/text documents into `config/rag_knowledge/shared/` or `config/rag_knowledge/<agent_id>/`.

### 2. Core Engine & Built-ins (`engine/`) — *Do Not Modify During Configuration*
The `engine/` directory contains the core Python runtime and immutable system definitions. Avoid modifying files in `engine/` unless performing a deliberate architectural refactoring:
- **System Agents & Orchestrator ([`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json))**: Engine equivalent of `agent_settings.json` for internal system agents (`Orchestrator`, `Discussion_Scribe`, `Intent_Extractor`, `Language_Detector`, `Pricing_Agent`, `Thread_Notes_Agent`, `Usage_Agent`, `InternalRevisor`, `List_Skills`).
- **Built-in System Prompts ([`engine/prompts/system_agents/`](file:///engine/prompts/system_agents) & [`engine/prompts/shared/`](file:///engine/prompts/shared))**: Core prompt assembly rules, nomination syntax, and system agent instructions.
- **Built-in Control & Modifier Skills ([`engine/skills/builtin/`](file:///engine/skills/builtin))**: Core orchestration skills (`control_plane`, `first`, `rounds`, `max_words`, `min_words`, `open_debate`, `grant_skill`, `stop_discussion`, etc.).
- **Seed Workflows ([`engine/workflows/`](file:///engine/workflows))**: Factory-default workflow templates used to seed and validate `config/workflows/`.

### 3. Runtime & Slack UI State (`data/`) — *Managed Automatically at Runtime*
The `data/` directory stores live databases, caches, audit logs, and Slack UI state:
- **Slack App Home State ([`data/state/app_settings.json`](file:///data/state/app_settings.json))**: Stores runtime toggles (circuit breaker thresholds, daily USD budget, Scribe toggle, workspace baseline context). **Modify these via the Slack App Home UI rather than editing the JSON file directly.**
- **SQLite State Engine (`data/izvor.db`)**, **Audit Logs (`data/audit/`)**, **Backups (`data/backups/`)**, and **Transcripts (`data/flat-histories/`)**.

---

### ⚙️ System Settings (`config/settings/system_settings.json`)

System operational parameters, security guardrails, and timeouts are managed by [`SystemSettingsManager`](file:///engine/orchestration/system_settings.py) (backed by [`JsonSettingsStore`](file:///engine/orchestration/settings_store.py)). These settings can be modified live without server restarts:

| Setting Key | Default | Description |
|---|---|---|
| `log_level` | `"INFO"` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `debate_turn_pacing_seconds` | `3.0` | Delay between consecutive agent turns to prevent flooding. |
| `tool_call_loop_timeout_seconds` | `60.0` | Maximum wall-clock execution time for iterative tool calling loops. |
| `slack_post_pause_seconds` | `2.0` | Brief pause before posting to Slack to allow human visual parsing. |
| `max_file_size_mb` | `50.0` | Maximum allowable file size for uploads and workspace parsing. |
| `max_write_bytes` | `1048576` | Hard ceiling in bytes (1 MB) for individual `write_document` workspace writes. |
| `strict_privacy_mode_enabled` | `true` | Enforces privacy boundaries and PII masking. |
| `internal_revisor_enabled` | `true` | Enables inline `InternalRevisor` quality/compliance checks at the end of agent turns. |
| `guardrail_fail_mode` | `"open"` | Semantic jailbreak guardrail fallback mode (`"open"` allows turn with warning, `"closed"` blocks execution and logs `GUARDRAIL_FAIL_CLOSED_BLOCKED`). |
| `guardrail_embedding_cache_size` | `500` | LRU cache capacity for prompt embedding similarity calculations. |
| `max_discussion_turns` | `20` | Hard ceiling for total agent turns in a single thread deliberation. |
| `max_tool_iterations` | `3` | Maximum tool execution iterations per single agent turn. |
| `tool_call_num_retries` | `2` | Number of retries permitted for transient tool invocation failures. |
| `word_limit_grace_buffer` | `50` | Word count buffer above `max_words` before mechanical truncation is applied. |
| `thread_cleanup_interval_seconds` | `600.0` | Frequency in seconds for pruning idle in-memory thread states (10 minutes). |
| `audit_integrity_check_interval_seconds` | `43200.0` | Frequency in seconds for verifying the cryptographic hash chain of audit logs (12 hours). |
| `pricing_cache_ttl_days` | `7` | Cache lifespan in days for LLM token pricing benchmarks. |
| `slack_file_download_timeout_seconds` | `30.0` | Timeout in seconds for downloading attached files from Slack. |
| `web_search_timeout_seconds` | `10.0` | Timeout in seconds for outbound DuckDuckGo MCP web searches. |
| `web_fetch_timeout_seconds` | `12.0` | Timeout in seconds for fetching web pages via MCP scraper. |
| `db_busy_timeout_seconds` | `5.0` | SQLite lock wait timeout in seconds (5,000ms) before raising database busy error. |
| `llm_max_retries` | `3` | Max automatic retries for transient LiteLLM provider errors. |
| `llm_retry_backoff_base` | `1.5` | Exponential backoff multiplier base for LLM retries. |
| `slack_rate_limit_per_second` | `10.0` | Outbound Slack API token bucket refill rate per second. |
| `slack_rate_limit_burst` | `15.0` | Maximum burst capacity for Slack API token bucket. |
| `default_fast_model` | `"gemini/gemini-3.8-flash"` | System-level fallback model for fast routing/parsing tasks. |
| `default_reasoning_model` | `"mistral/mistral-large-latest"` | System-level fallback model for deep deliberation tasks. |
| `pricing_benchmark_url` | `"https://artificialanalysis.ai/models"` | Endpoint for remote LLM token pricing benchmarks. |
| `web_search_endpoint` | `"https://html.duckduckgo.com/html/"` | Target endpoint for outbound web searches. |
| `web_fetch_user_agent` | Izvor Bot string | HTTP `User-Agent` header for outbound web fetching. |
| `web_security` | `{...}` | Outbound web safety rules (`max_url_length: 2048`, `max_query_length: 1024`, `max_redirects: 5`, `max_body_bytes: 2097152`, `allowed_ports: [80, 443]`, `allowed_domains: []`, `strip_query_on_untrusted_domains: true`, `max_fetches_per_turn: 10`). |
| `security_burst_limits` | `{...}` | Sliding-window rate limiters for failed authentication (`auth_failure_window_seconds: 60.0`, `auth_failure_burst_limit: 5`, `global_auth_failure_burst_limit: 25`, `max_tracker_entries: 1000`). |
| `convergence_consecutive_threshold` | `2` | Number of consecutive `signal_consensus_convergence` calls required before triggering the Challenger/Arbitrator fast-forward gate. |
| `dialectical_stress_test` | `{...}` | Dialectical stress-test parameters (`enabled: true`, `consecutive_threshold: 2`, `max_stress_rounds: 1`). |
| `retention_checkpoints_days` | `7` | Retention window in days before resolved `turn_checkpoints` rows are deleted. |
| `retention_traces_days` | `30` | Retention window in days before `turn_traces` telemetry spans are deleted. |
| `retention_event_dedup_days` | `1` | Retention window in days before `event_dedup` rows are purged. |
| `workspace_write_blacklist_additional` | `[]` | Additional filename patterns blocked from workspace `write_document` operations. |
| `rag` | `{...}` | Semantic chunking, LanceDB dimensions, and embedding configuration. |

---

## 🖥️ Running the Application

### Development Mode

Run the server with Uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Windows Proactor Loop Notice

On Windows platforms, Python's Proactor event loop can occasionally stall when no incoming HTTP sockets are active. Izvor automatically starts a background heartbeat coroutine [`keep_loop_alive`](file:///main.py#L50-L57) to keep the loop responsive without measurable CPU overhead.

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

## 🌐 Public Ingress for Slack Webhooks

Because Slack delivers event notifications via HTTP POST to public URLs, your local development server must be reachable from the internet.

Use a tunnel such as `ngrok` or `cloudflared`:

```bash
ngrok http 8000
```

Note your public HTTPS URL (e.g., `https://abcdef123.ngrok-free.app`). You will use this in the [Slack App Setup Guide](file:///docs/getting-started/slack-app-setup.md).
