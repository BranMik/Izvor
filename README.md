# CaiLogics Multi-Agent Slack Hub (SlackAgenticPanel)

An enterprise-ready, asynchronous multi-agent deliberation platform that connects Slack workspaces (via the Slack Events API, Interactive Components, and App Home) to specialized LLM personas using LiteLLM and Model Context Protocol (MCP) tools.

---

## 🏗️ Architecture Overview

```mermaid
graph TD
    User([Slack User]) -->|Mentions / Slash Commands| SlackAPI[Slack Platform]
    SlackAPI -->|HTTP Webhook POST /slack/events| FastAPIApp[FastAPI Server]

    subgraph Core Pipeline
        FastAPIApp --> Signature[Slack Signature Verification]
        Signature --> EventDedup[Event Deduplication & Generation Check]
        EventDedup --> DirectiveParser[Directive & Tag Parser]
        DirectiveParser --> TurnPipeline[Turn Execution Pipeline]
    end

    subgraph Orchestration Layer
        TurnPipeline --> AgentSettings[AgentSettingsManager - SSOT]
        TurnPipeline --> ThreadState[ThreadStateManager - TTL Eviction]
        TurnPipeline --> Topology[SandwichOrchestrator - Sandwich / Fixed]
        TurnPipeline --> EntityResolver[Entity Resolver - Persona Matching]
    end

    subgraph Tooling & Governance Layer
        TurnPipeline --> MCPTools[Safe MCP Workspace & Web Tools]
        TurnPipeline --> InPlaceStatus[5s Delayed Progress Update & In-Place Edit]
        TurnPipeline --> SpendGov[Spend Governance & Circuit Breakers]
        TurnPipeline --> PricingService[Pricing Research Agent & 7-Day Cache]
    end

    subgraph LLM Layer
        TurnPipeline --> LiteLLM[LiteLLM Provider Router]
        LiteLLM --> Models[OpenRouter / Anthropic / OpenAI / Gemini / Mistral]
    end

    TurnPipeline -->|Final In-Place Edit / Dispatch| SlackAPI
```

---

## ✨ Key Features

### 1. Multi-Persona Discussion Topologies
- **🥪 Sandwich Mode**: Starts with designated initial speakers (*Start Chain*), flows into an arbitrary/nominated pool (*Middle Pool*), and terminates with a designated summarizer/synthesizer (*End Chain*).
- **🎯 Fixed Mode**: Executes a strict, predetermined sequential chain of agents.
- **Dynamic Nomination**: Personas nominate their peers naturally using bracketed identifiers (e.g., `[TechLead]`, `[Architect]`).

### 2. Thread State & Memory Safety
- **Bounded In-Memory State**: Automatic time-to-live (TTL) thread tracking (`cleanup_expired_threads`) running every 10 minutes and opportunistically evicting inactive threads (`max_tracked_threads = 500`).
- **Generation-Based Superseding**: New human inputs in an active thread increment thread generation, automatically aborting stale background agent tasks without race conditions.
- **Graceful Task Cancellation**: Fast shutdown and thread-safe cancellation guards throughout all pipeline stages.

### 3. Tool Execution & In-Place Slack Progress
- **Wall-Clock Timeout**: LLM tool calling loops are guarded with a configurable timeout (`TOOL_CALL_LOOP_TIMEOUT_SECONDS`, default: 60s).
- **Delayed Progress Indicator**: If tool execution exceeds 5 seconds, an intermediate status message is posted under the agent persona (e.g., `_🔍 *Visionary* pretražuje web..._`). Fast tool calls (<5s) complete cleanly without Slack UI flicker.
- **In-Place Message Update**: Upon completion, the intermediate placeholder is edited in-place with the final response text, leaving zero clutter in the Slack thread.
- **Safe MCP Workspace Boundary**: Path resolution restricts filesystem tools to the configured workspace root.

### 4. Spend Governance & Model Pricing Service
- **Per-Thread Token Tracking**: Logs prompt and completion tokens per model after every turn.
- **Autonomous Pricing Researcher**: Dispatches a web-searching agent to query and benchmark per-token API costs from official provider pricing tables.
- **7-Day Disk-Backed Cache**: Caches model pricing in `data/model_pricing_cache.json` with baseline fallbacks.
- **Circuit Breaker**: Automatic thread cut-off when expenses exceed a configurable USD threshold (configured globally or via `/cb`).

### 5. Interactive Slack App Home & Settings SSOT
- **Live Configuration**: Real-time toggling of enabled debate personas, orchestration modes, and circuit breaker thresholds directly from the Slack App Home tab.
- **Failover & Emergency Backups**: Atomic file writes to `data/agent_settings.json` with automatic 7-day emergency backups and non-destructive failover.

---

## 📁 Project Structure

```
SlackAgenticPanel/
├── agents/                     # Specialized utility agents (Usage, Thread Notes)
├── core/                       # Core configuration, models, domain entities, and LLM retry utilities
│   ├── config.py               # Central environment variables & pacing defaults
│   ├── domain.py               # Domain dataclasses (SessionContext, Turn, SpendMetrics)
│   ├── llm_utils.py            # Exponential backoff retry engine & error classification
│   ├── models.py               # OrchestrationMode enum, FileContext, CircuitBreakerConfig
│   └── slack_security.py       # HMAC-SHA256 signature verification
├── data/                       # Persistent JSON state and automated backups
├── nlp/                        # Regex directive extractors, NL patterns, and message builder
├── orchestration/              # Core routing, turn pipeline, thread state, and settings SSOT
│   ├── agent_router.py         # Main execution entrypoint (process_and_respond)
│   ├── agent_settings.py       # AgentSettingsManager (SSOT)
│   ├── nomination.py           # Persona tag resolution and turn-taking rules
│   ├── thread_state.py         # ThreadStateManager with TTL-based memory eviction
│   └── turn_pipeline.py        # 5-stage pipeline with tool timeouts and in-place updates
├── pipeline/                   # Modular pipeline subpackages (EntityResolver, Orchestrator, etc.)
├── prompts/                    # Persona system prompts and system agent definitions
├── services/                   # Slack async web client, Safe MCP tools, and Pricing service
├── tests/                      # Comprehensive pytest test suite (198 passing unit/integration tests)
├── .env.example                # Sample environment configuration
├── main.py                     # FastAPI application and webhook listener
├── requirements.txt            # Pinned dependency requirements
└── README.md                   # Project documentation
```

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.11+ (Python 3.14+ fully supported)
- A Slack App with Bot Token Scopes: `chat:write`, `chat:write.customize`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `files:read`, `app_mentions:read`.

### 2. Installation

```bash
# Clone the repository
git clone https://github.com/BranMik/SlackAgenticPanel.git
cd SlackAgenticPanel

# Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# or: .venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration

Create a `.env` file in the project root based on `.env.example`:

```env
# Slack Credentials
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_SIGNING_SECRET=your-slack-signing-secret

# LLM Provider API Keys (use what your configured models require)
OPENROUTER_API_KEY=your-openrouter-key
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
GEMINI_API_KEY=your-gemini-key
MISTRAL_API_KEY=your-mistral-key

# Server Settings
PORT=8000
MCP_WORKSPACE_DIR=.

# Pacing & Timeouts (optional overrides)
DEBATE_TURN_PACING_SECONDS=3.0
SLACK_POST_PAUSE_SECONDS=2.0
TOOL_CALL_LOOP_TIMEOUT_SECONDS=60.0
TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS=5.0
```

### 4. Running Locally

```bash
# Run with uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Expose the webhook via a Cloudflare Tunnel, ngrok, or reverse proxy:
```bash
cloudflared tunnel --url http://localhost:8000
```
Set your Slack Event Subscriptions Request URL to:
`https://your-public-url.com/slack/events`

---

## 🧪 Running Tests

The test suite contains 198 unit and integration tests covering the router, state manager, turn pipeline, tool loop timeout, pricing service, and security layers.

```bash
# Run all tests
pytest

# Run a specific test suite
pytest tests/test_tool_loop_and_updates.py
pytest tests/test_agent_settings_ssot.py
pytest tests/test_models_and_managers.py
```

---

## 💬 Usage & Directives

### Triggering Discussions
- **Mention**: `@BotName What are the architectural trade-offs of microservices vs modular monoliths?`
- **Explicit Target**: `@BotName [Visionary] Give your perspective first.`
- **Exclusion**: `@BotName bez Strategista (Exclude Strategist)`
- **Word Limits**: `@BotName max 100 riječi (Keep answers under 100 words)`
- **History Window**: `@BotName zadnje 3 poruke (Limit context window to last 3 messages)`
- **Directives & Commands**:
  - `/cb on` / `/cb off` / `/cb 2.50` — Control the thread expense circuit breaker.
  - `/stop-discussion` or posting `stop` / `prekini` — Halts active deliberation immediately.
  - `/read-file: filename.py` — Grants authorization and attaches specified file content to agent prompts.

---

## 🔒 Security

- **Slack Signature Verification**: All incoming webhooks are strictly verified via HMAC-SHA256 against `SLACK_SIGNING_SECRET` with 5-minute replay attack mitigation.
- **Filesystem Boundary Guard**: Safe path resolution ensures MCP filesystem operations cannot traverse outside `MCP_WORKSPACE_DIR`.
- **Secret Protection**: `.gitignore` strictly protects `.env*` files from accidental repository commits.
