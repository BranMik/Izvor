# Izvor — Multi-Agent Dialectical AI Advisory and Orchestration Platform for Slack

<p align="center">
	<img src="izvor_logo.png" alt="Logo" width="200">
</p>

## Table of Contents

1. [System Definition](#1-system-definition)
2. [Core Features](#2-core-features)
   - [2.1 Multi-Persona Deliberation Topologies](#21-multi-persona-deliberation-topologies)
   - [2.2 Context Engineering, Epistemic Integrity & Skills](#22-context-engineering-epistemic-integrity--skills)
   - [2.3 Security, Privacy & Spend Governance](#23-security-privacy--spend-governance)
3. [Architecture Overview](#3-architecture-overview)
4. [Main System Flows](#4-main-system-flows)
5. [Quick-Start Guide](#5-quick-start-guide)
6. [Usage Guide](#6-usage-guide)
   - [6.1 Triggering the Application](#61-triggering-the-application)
   - [6.2 Invoking & Steering Agents](#62-invoking--steering-agents)
   - [6.3 Calling Slash Directives](#63-calling-slash-directives)
   - [6.4 Inferred Skills (`auto_detect: true`)](#64-inferred-skills-auto_detect-true)
7. [Skills](#7-skills)
   - [7.1 Configurable Agent Tool Skills (`config/skills/`)](#71-configurable-agent-tool-skills-configskills)
   - [7.2 Built-in Orchestrator & Prompt-Modifier Skills (`engine/skills/builtin/`)](#72-built-in-orchestrator--prompt-modifier-skills-engineskillsbuiltin)
8. [Configuration & Customization Quick Guide](#8-configuration--customization-quick-guide)
   - [8.1 Adding & Modifying Debate Agents (`config/settings/agent_settings.json` & `config/personas/`)](#81-adding--modifying-debate-agents-configsettingsagent_settingsjson--configpersonas)
   - [8.2 Adding & Modifying Skills (`config/skills/`)](#82-adding--modifying-skills-configskills)
   - [8.3 Connecting External MCP Servers (`mcp_servers` + `SKILL.md`)](#83-connecting-external-mcp-servers-mcp_servers--skillmd)
   - [8.4 Adding & Modifying Workflows (`config/workflows/`)](#84-adding--modifying-workflows-configworkflows)
   - [8.5 Adding & Updating RAG Knowledge Documents (`config/rag_knowledge/`)](#85-adding--updating-rag-knowledge-documents-configrag_knowledge)
   - [8.6 Managing Agent Workspace Resources (`agents_resources/`)](#86-managing-agent-workspace-resources-agents_resources)
9. [Debugging & Troubleshooting](#9-debugging--troubleshooting)
10. [Documentation Hub](#10-documentation-hub)
11. [License](#11-license)
12. [Disclaimer & Limitation of Liability](#12-disclaimer--limitation-of-liability)

---

## 1. System Definition

**Izvor** (`v0.9.0`, Python `>=3.11`) is an asynchronous **Multi-Agent Dialectical Deliberation and Orchestration Platform for Slack** built on FastAPI, LiteLLM, SQLite (WAL mode), LanceDB + Tantivy hybrid RAG, Microsoft Presidio privacy filters, and the Model Context Protocol (MCP).

Instead of relying on a single generic chatbot that is prone to sycophancy or narrow framing, Izvor orchestrates structured, multi-turn discussions among **multiple specialized debate personas** (16 default OOB, such as *Visionary*, *Innovator*, *Sceptic*, *Security Architect*, *ROI Strategist*, *Legal Advisor*, and *Tactician*) and **5 built-in system agents** (*Discussion Scribe*, *Internal Revisor*, *Dialectical Challenger*, *Arbitrator*, and *ListSkills*) directly inside Slack threads. Personas critique one another, dynamically nominate peers, invoke sandboxed tools, and synthesize actionable conclusions under strict cost, privacy, and security guardrails.

---

## 2. Core Features

### 2.1 Multi-Persona Deliberation Topologies
Managed by [workflow_manager.py](engine/config_store/workflow_manager.py) and [sandwich.py](engine/pipeline/orchestrator/sandwich.py), Izvor supports multiple discussion topologies:
- **Sandwich Mode (`sandwich`)**: Opens with a sequential *Start Chain* for problem framing, transitions into an open *Middle Pool* with autonomous peer nominations (`/[@Agent]` or `nominate_next_speaker`), and concludes with a sequential *End Chain* for synthesis.
- **Parallel Dialectic / Scatter-Gather (`scatter_gather`)**: Executes Turn 1 framing, fans out concurrent specialist branches via `asyncio.TaskGroup` using isolated conversation histories (eliminating sequential anchoring bias) with atomic budget pre-reservations, and gathers all perspectives for final *End Chain* synthesis.
- **Fixed Chain (`fixed`) & Custom Ad-Hoc Flows**: Deterministic ordered sequences from preset workflows or explicit user tags in Slack (e.g., `/[Visionary] /[SecurityArchitect]`).
- **Open Debate (`/open_debate`)**: Starts with a designated speaker and opens the floor to the entire active panel without fixed start/end chains.
- **Adversarial Convergence & Fast-Forward**: Tracks consecutive agreement streaks (`signal_consensus_convergence`). Before allowing premature consensus, Izvor injects a mandatory **Challenger → Arbitrator** stress-test gate; the *Arbitrator* either upholds consensus (**Verdict A**, fast-forwarding to the *End Chain*) or exposes flaws (**Verdict B**, reopening specialist debate).

### 2.2 Context Engineering, Epistemic Integrity & Skills
- **3-Tier Thread Memory**: Combines (1) *Episodic Summaries* maintained by the *Discussion Scribe* ([scribe.py](engine/pipeline/discussion_scribe/scribe.py)), (2) *Shared Round Evidence* deduplicating read-only tool outputs across turns ([evidence_store.py](engine/orchestration/state/evidence_store.py)), and (3) *Sliding-Window History* ([message_builder.py](engine/nlp/message_builder.py)).
- **Two-Stage Internal Revisor**: After every turn, [revisor_service.py](engine/orchestration/revisor_service.py) screens the agent's claims against authoritative ground truth (`<initial_user_prompt>` and `<retrieved_evidence>`) with a fast model and confirms any flagged hallucination with a stronger reasoning model before posting a warning.
- **Declarative Skills & Progressive Disclosure**: Loads **22 declarative skills** — **4 configurable agent tool skills** ([config/skills/](config/skills): `web_research`, `document_reader`, `document_authoring`, `knowledge_base_rag`), **18 built-in directive/control-plane skills** ([engine/skills/builtin/](engine/skills/builtin), including admin GDPR erasure `/forget_thread`), and external **MCP servers** ([client_manager.py](engine/services/mcp/client_manager.py)). `SkillRegistry` uses lazy initialization (`auto_load=False` + explicit startup `reload()`); agents start with `load_skill` and unlock authorized tool schemas on demand, enforced by fail-closed `ToolPrincipal` ACLs.
- **Hybrid Local RAG**: Combines dense vector embeddings in LanceDB with Tantivy BM25 full-text search using Reciprocal Rank Fusion ([service.py](engine/services/rag/service.py)), kept in sync via incremental SHA-256 manifest scanning, collision-free SHA-256 `sanitize_id`, code-fence-aware Markdown parsing, shared per-document PII `token_mapping`, and `__meta__.config_hash` auto-reindexing over shared and agent-scoped documents in [config/rag_knowledge/](config/rag_knowledge) ([sync_engine.py](engine/services/rag/sync_engine.py)).

### 2.3 Security, Privacy & Spend Governance
- **Defense-in-Depth Security**: HMAC-SHA256 Slack webhook verification ([slack_security.py](engine/core/slack_security.py)), hardened prompt-injection & indirect tool-output guardrails ([guardrail.py](engine/services/security/guardrail.py) with zero-cost heuristic pre-filter, capped sliding-window LanceDB embeddings, and `screen_untrusted_tool_output()`), two-way configurable Presidio NER + regex PII masking ([pii_masker.py](engine/services/security/pii_masker.py)), DNS-pinned SSRF/Slowloris-safe web fetching ([web_tools.py](engine/services/web_tools.py)), and non-repudiable HMAC-chained monthly JSONL audit logs with `key_id` keyring rotation (`AUDIT_HMAC_KEYS_JSON`) anchored in SQLite ([audit_logger.py](engine/services/audit_logger.py)), alongside REST GDPR erasure (`DELETE /api/threads/{channel_id}/{thread_ts}`).
- **Spend Governance & Durable Checkpointing**: Tracks token and USD spend per turn ([pricing_service.py](engine/services/pricing_service.py)), enforces per-thread and daily USD circuit breakers ([circuit_breaker.py](engine/pipeline/spend_governance/circuit_breaker.py)), and serializes mid-turn `TurnCheckpoint` snapshots ([turn_checkpoint_store.py](engine/services/turn_checkpoint_store.py)) for Human-in-the-Loop (HITL) tool approvals and budget pauses.
- **Slack App Home Control Plane**: Interactive UI ([slack_home/](engine/services/slack_home/__init__.py)) for Slack admins to configure workflows, agent models/skills, system settings, and budgets with rolling JSON backups, Block Kit sanitization, per-user publish locking, and SHA-256 view deduplication.

<p align="center">
	<img src="slack_app_home.png" alt="Slack App Home Control Plane" width="1000">
</p>
<p align="center">
	<img src="slack_debate.png" alt="Multi-Agent Slack Debate" width="1000">
</p>

---

## 3. Architecture Overview

Izvor is structured around a **3-Pillar Physical Layout** ([paths.py](engine/core/paths.py)) and a strict **5-Tier Unidirectional DAG Architecture** (`150` Python files across `11` engine packages):

1. **`config/` (Implementer Configuration Pillar)**: Customizable settings ([agent_settings.json](config/settings/agent_settings.json), [system_settings.json](config/settings/system_settings.json)), 16 persona Markdown prompts (`config/personas/`), agent tool skills (`config/skills/`, 4 OOB), custom workflows (`config/workflows/`), and RAG documents (`config/rag_knowledge/`).
2. **`engine/` (Immutable Core Engine Pillar)**: 150 Python modules across 5 DAG tiers enforced by AST checks in [test_architecture_layers.py](tests/unit/test_architecture_layers.py):
   - **Tier 1 (`engine/core/`)**: Zero-upward-import foundation (`database.py`, `domain.py`, `models.py`, `paths.py`, `llm_utils.py`, `slack_security.py`, `logging_filters.py`).
   - **Tier 1.5 (`engine/config_store/`)**: Single-Source-of-Truth config & preset persistence (`WorkflowManager`, `AgentSettingsManager`, `SystemSettingsManager`, `AppSettingsManager`, `JsonSettingsStore`, `ConfigIntegrityValidator`).
   - **Tier 2 (`engine/services/`, `engine/skills/`, `engine/nlp/`, `engine/prompts/`, `engine/pipeline/`)**: Directive parsing, Sandwich/Scatter-Gather routing, hybrid RAG (`LanceDB` + `Tantivy` BM25), MCP client manager, PII masking, HITL, pricing, audit logging, and `ThreadSkillsManager`.
   - **Tier 3 (`engine/orchestration/`, `engine/agents/`)**: `DiscussionLoop`, the 5-stage `TurnPipeline` (`ToolLoopRunner`, `ControlPlaneHandler`, `TurnFinalizer`), `ThreadStateManager`, `RevisorService`, and background support agents (`ThreadNotesAgent`, `UsageAgent`).
   - **Tier 4 (`engine/api/`, `engine/adapters/slack/`, [main.py](main.py))**: FastAPI server lifespan (`WEB_CONCURRENCY > 1` fail-fast + graceful task drain), Admin & Public REST API (`engine/api/`, `/api/*` incl. `DELETE /api/threads/{channel_id}/{thread_ts}`), Slack webhook ingress, and `SlackWorkspaceAdapter`.
3. **`data/` (Runtime & UI State Pillar)**: Embedded SQLite WAL database (`data/izvor.db` with 11 tables), Slack App Home state (`data/state/app_settings.json`), Presidio exclusions (`data/state/presidio_skill_exclusions.json`), hash-chained audit logs (`data/audit/`), Scribe thread notes (`data/thread_notes/`), thread downloads (`data/downloads/`), exported flat histories (`data/flat-histories/`), LanceDB vector indices (`data/.rag_cache/`), pricing cache (`data/pricing/`), and rolling backups (`data/backups/`).

---

## 4. Main System Flows

### Diagram 1: End-to-End Request & 5-Stage Turn Pipeline Flow

```mermaid
flowchart TD
    User(["Slack User / Admin"]) -->|"Mentions, Slash Directives, Interactive Actions"| Slack["Slack Platform"]
    Slack -->|"POST /slack/events, /slack/actions or /slack/interactivity"| Ingress["FastAPI Ingress & HMAC Guard (main.py, slack_security.py)"]
    Ingress -->|"SQLite Event Dedup (+ LRU Fallback) & 200 OK"| Router["Agent Router & DiscussionLoop (agent_router.py, discussion_loop.py)"]

    subgraph Pipeline["5-Stage TurnPipeline (turn_pipeline.py)"]
        S1["Stage 1: ValidationStage\nHeuristic + Windowed Guardrail, Hybrid Directives,\n/forget_thread GDPR Purge & Candidate Resolution"]
        S2["Stage 2: ResolutionStage\nAgent config lookup, generation check &\nSpend Governance circuit breaker"]
        S3["Stage 3: ContextPrepStage\n3-Tier Memory assembly, XML prompt building,\nskill filtering & word constraints"]
        S4["Stage 4: ExecutionStage & ToolLoopRunner\nPII masking, LiteLLM completion, progressive\nload_skill & Indirect Tool-Output Screening"]
        S5["Stage 5: PostRecordStage\nMechanical word cutoff, mrkdwn formatting,\nnomination repair & Slack dispatch"]
        S1 --> S2 --> S3 --> S4 --> S5
    end

    Router --> S1
    S4 <-->|"acompletion / aembedding"| LiteLLM["LiteLLM Multi-Provider Gateway\n(Gemini, Claude, OpenAI, Mistral)"]
    S4 <-->|"ToolPrincipal ACLs + Indirect Screening"| Tools["Sandboxed Tools, Hybrid RAG & MCP Servers"]
    S2 & S4 & S5 <-->|"ACID WAL Transactions"| SQLite[("SQLite State Engine\n(data/izvor.db - 11 Tables)")]
    S5 --> Revisor["Two-Stage InternalRevisor Barrier\n(Fast Screen -> Reasoning Confirm)"]
    Revisor -->|"Next Turn / Peer Handoff"| Router
    S5 -->|"In-Place Edit / Thread Post + Scribe & Usage Updates"| Slack
```

### Diagram 2: Multi-Agent Deliberation Topologies & Convergence Stress-Test Flow

```mermaid
flowchart LR
    Start(["Thread Round Start"]) --> Mode{"Active Workflow\nTopology"}

    Mode -->|"fixed"| Fixed["Fixed Chain\nSequential execution of\nconfigured agent chain"]
    Mode -->|"scatter_gather"| Scatter["Turn 1 Framing -> Parallel Scatter\nConcurrent asyncio.TaskGroup branches\nwith isolated history & budget reservation"]
    Scatter --> Gather["Gather & Synthesis\nDeterministic merge into\nEnd Chain synthesizer"]

    Mode -->|"sandwich / open_debate"| StartChain["Start Chain\nInitial problem framing"]
    StartChain --> MiddlePool["Middle Pool Deliberation\nPeer nominations & tool investigation"]

    MiddlePool --> ConvCheck{"Consecutive\nConvergence\nThreshold Met?"}
    ConvCheck -->|"No"| MiddlePool
    ConvCheck -->|"Yes"| StressTest["Dialectical Stress-Test Gate\nInject Challenger -> Arbitrator"]

    StressTest --> Verdict{"Arbitrator\nVerdict"}
    Verdict -->|"Verdict B: Flaw Exposed"| MiddlePool
    Verdict -->|"Verdict A: Consensus Upheld"| EndChain["End Chain Synthesis\nConcluding wrap-up"]

    Fixed & Gather & EndChain --> Scribe["Discussion Scribe & Flat History\nLive pinned brief, final conclusion note\n& Markdown transcript export"]
```

---

## 5. Quick-Start Guide

Because Slack sends webhooks (`/slack/events`, `/slack/interactivity`) and fetches custom persona avatars (`/avatars/*`) over public HTTPS, set up your **Cloudflare Tunnel** and **Slack App** before launching your first deliberation.

### Step 1: Clone & Install Dependencies

```bash
git clone https://github.com/BranMik/Izvor.git izvor
cd izvor

# Create & activate virtual environment (Windows PowerShell shown; use source .venv/bin/activate on Linux/macOS)
python -m venv .venv
.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
```
*(See the full [Installation & Configuration Guide](docs/getting-started/installation.md) for development dependencies and `system_settings.json` tuning.)*

### Step 2: Set Up Cloudflare Tunnel (Public HTTPS Ingress)

Expose local port `8000` over HTTPS using `cloudflared` (full instructions in [Public Ingress & Cloudflare Tunnel Setup](docs/getting-started/installation.md#public-ingress--cloudflare-tunnel-setup)):

- **Option A — Persistent Named Tunnel (Recommended if you have a Cloudflare domain)**:
  ```bash
  cloudflared tunnel login
  cloudflared tunnel create izvor-hub
  cloudflared tunnel route dns izvor-hub izvor.yourdomain.com
  cloudflared tunnel run --url http://localhost:8000 izvor-hub
  ```
- **Option B — Instant Ephemeral Tunnel (No account required)**:
  ```bash
  cloudflared tunnel --url http://localhost:8000
  ```
Copy your public HTTPS tunnel hostname (e.g., `https://izvor.yourdomain.com` or `https://<random>.trycloudflare.com`).

### Step 3: Configure `.env` & Start the Izvor Server

1. Copy [`.env.example`](.env.example) to `.env` and fill in your LLM API keys (`GEMINI_API_KEY`, `MISTRAL_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.), `ADMIN_PASSWORD`, and your tunnel URL as `BASE_AVATAR_URL`:
   ```ini
   PORT=8000
   BASE_AVATAR_URL=https://<your-tunnel-hostname>/
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=change-me-to-a-secure-password
   GEMINI_API_KEY=your-gemini-api-key
   MISTRAL_API_KEY=your-mistral-api-key
   ```
   Your `.env` should contain at least the API keys for the models configured for system agents and platform defaults in [`config/settings/system_settings.json`](config/settings/system_settings.json) (`default_fast_model`, `default_reasoning_model`, and `rag.embedding_model`, which default to `GEMINI_API_KEY` and `MISTRAL_API_KEY` — also used by built-in system agents such as *DiscussionScribe*, *InternalRevisor*, *Challenger*, *Arbitrator*, and *ListSkills*). Default panel agents use Mistral, Gemini, Anthropic, and OpenAI models; if an agent's configured key is missing from `.env`, it automatically falls back to `default_reasoning_model` (if its key is present) or the first available model in `.env`, adding a brief fallback footer on the Slack message.
2. Start the FastAPI server (so it can answer Slack's webhook `url_verification` challenge in Step 4):
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

### Step 4: Create & Install the Slack App (2-Minute App Manifest Setup)

Follow the complete [Slack App Setup Guide](docs/getting-started/slack-app-setup.md):
1. Go to the [Slack API App Console](https://api.slack.com/apps) → **Create New App** → **From an app manifest**.
2. Paste the ready-made JSON manifest from [docs/getting-started/slack-app-setup.md](docs/getting-started/slack-app-setup.md#app-manifest-configuration-recommended), replacing `https://<your-domain>` with your Cloudflare Tunnel hostname:
   - **Event Subscriptions Request URL**: `https://<your-tunnel-hostname>/slack/events`
   - **Interactivity Request URL**: `https://<your-tunnel-hostname>/slack/interactivity`
1. Install the app to your workspace, then copy your **Bot User OAuth Token** (`xoxb-...`), **Signing Secret** (after clicking "App Settings"), and your Slack Member ID (from user profile in the Slack UI) into `.env`:
   ```ini
   SLACK_BOT_TOKEN=xoxb-your-bot-user-oauth-token
   SLACK_SIGNING_SECRET=your-slack-signing-secret
   SLACK_ADMIN_USER_IDS=U0123456789
   ```
2. Restart `uvicorn main:app --host 0.0.0.0 --port 8000 --reload`, verify `curl http://localhost:8000/health`, and invite `@Izvor` to any Slack channel (`/invite @Izvor`)!

---

## 6. Usage Guide

Once the server is running and `@Izvor` is invited to a channel (`/invite @Izvor`), you interact with the platform directly inside Slack channels, threads, and direct messages.

### 6.1 Triggering the Application

Defined in [`engine/adapters/slack/events.py`](engine/adapters/slack/events.py), Izvor is triggered in Slack channels by mentioning `@Izvor` (or `<@BOT_ID>`) — or directly in a Slack DM (`D*` channel). Plain channel chatter without a mention is ignored, and unprefixed mentions of the lowercase word `"izvor"` do not cause false triggers.

> [!NOTE]
> **Always prefix channel prompts, `/[Agent]` tags, and `/directives` with `@Izvor`**:
> Slack intercepts messages starting with `/` as native Slack slash commands (`"... is not a valid command"`), and channel events without an `app_mention` may not trigger in Slack unless `@Izvor` is mentioned. Prefixing with `@Izvor` ensures reliable delivery in channels and threads:
> - **Panel Debate**: `@Izvor Should we migrate our session store from Redis to Postgres JSONB?`
> - **Targeted Agent**: `@Izvor /[SecurityArchitect] What encryption controls are required for storing PII under GDPR?`
> - **Slash Directive**: `@Izvor /rag "rollback runbook" What are our production rollback steps?`

> [!TIP]
> **Interactive Steering, HITL & Pause Resumption**:
> - **Mid-Debate Interruption**: Replying in an active thread immediately increments the thread generation counter and gracefully cancels any in-flight turn so the next speaker follows your updated instruction.
> - **Human-in-the-Loop (HITL)**: Mutating tools such as `write_document` pause execution via `TurnCheckpointStore` and post **Approve** / **Deny** Block Kit buttons in Slack.
> - **Spend Circuit Breaker & Clarifications**: Reaching `circuit_breaker_threshold_usd` pauses the thread with a **Continue Sequence** button, and agents calling `request_user_clarification` pause until you reply.

---

### 6.2 Invoking & Steering Agents

Izvor resolves persona keys, usernames, and display names flexibly (e.g., `SecurityArchitect`, `Security Architect`, or `agent-10008` from [`config/settings/agent_settings.json`](config/settings/agent_settings.json)):

- **Default Multi-Agent Panel Debate** — runs the active workflow preset configured in Slack App Home / `agent_settings.json`:
  ```text
  @Izvor We need to choose between multi-region active-active Postgres and single-region + read replicas. Analyze tradeoffs.
  ```
- **Single-Agent Query (Single Turn)** — tagging **one** persona runs only that specialist's turn and clears the remaining queue:
  ```text
  @Izvor /[SecurityArchitect] Review this JWT rotation flow for replay risks.
  ```
- **Ad-Hoc Sequential Chain (`custom_flow`)** — tagging **multiple** personas runs a custom sequential flow across strictly those agents in order:
  ```text
  @Izvor /[Visionary] /[ROIStrategist] /[SecurityArchitect] /[LegalAdvisor] Evaluate launching an EU-hosted AI SaaS tier.
  ```
- **Custom Opening Speaker (`/first`)** — sets the opening speaker and continues in the active workflow order:
  ```text
  @Izvor /first Visionary How should we price our enterprise tier?
  ```
- **Open Dynamic Debate (`/open_debate`)** — sets the opening speaker and removes fixed start/end chains so agents dynamically nominate peers:
  ```text
  @Izvor /open_debate Innovator Propose new retention mechanics for our developer portal.
  ```
- **Include or Exclude Agents (`/include`, `/exclude`)** — dynamically modify the active panel for the thread:
  ```text
  @Izvor /include LegalAdvisor /exclude Sceptic Review our updated enterprise SLA draft.
  ```

---

### 6.3 Calling Slash Directives

Slash directives are parsed deterministically by [`DirectiveParser`](engine/pipeline/directive_parser/parser.py) with `origin: "slash"`, stripped cleanly from the prompt text, and can be combined in a single message. Explicit slash directives always take precedence over natural-language inferences.

- **Output Length & Context Slicing**:
  ```text
  @Izvor /max_words 120 /min_words 50 /history 10 Compare Kafka vs. NATS JetStream.
  ```
  - `/max_words N` (`/max`, clamped `1–2000`; `< 60` activates Compact Override Mode) and `/min_words N` (`/min`, clamped `1–1000`).
  - `/scribe_max_words N` and `/scribe_min_words N` (clamped `1–2000`) control the *Discussion Scribe*'s summary length.
  - `/history N` (clamped `0–50`; `/history 0` ignores prior messages while preserving the Scribe's episodic summary).
- **Debate Rounds & Immediate Stop**:
  ```text
  @Izvor /rounds 2 Debate build vs. buy for our internal feature store.
  ```
  ```text
  @Izvor /stop
  ```
  - `/rounds N` is clamped `1–5` (global thread cap `20` turns) and requires explicit slash syntax.
  - `@Izvor /stop` immediately halts automated agent turns in the thread.
- **Invoking Agent Tool Skills Explicitly**:
  ```text
  @Izvor /web_research "Anthropic vs Gemini pricing" Compare current API token pricing.
  @Izvor /rag "incident escalation" Summarize our on-call escalation policy.
  @Izvor /read_file architecture.pdf Identify single points of failure in the attached spec.
  @Izvor /document_authoring q4_summary.md Draft an executive summary from this thread.
  ```
  *(Uploading a Slack attachment or referencing its filename in the prompt also triggers `document_reader` automatically.)*
- **Directory Inspection & Slack Admin Directives** *(Admin commands require `SLACK_ADMIN_USER_IDS` membership **and** explicit slash syntax)*:
  ```text
  @Izvor /list_skills
  @Izvor /grant_skill web_research /[Visionary]
  @Izvor /grant_skill_for_all web_research knowledge_base_rag
  @Izvor /revoke_skill_for_all web_research
  @Izvor /revoke_constraint max_words
  @Izvor /forget_thread
  ```
  - Disabling active constraints via `off` (e.g., `@Izvor /max_words off`, `@Izvor /exclude off`, `@Izvor /include off`) also requires Slack Admin privileges.

---

### 6.4 Inferred Skills (`auto_detect: true`)

Instead of memorizing slash commands, users can express constraints and tool requests in natural language (in any language). During pre-flight parsing, [`IntentExtractor`](engine/nlp/intent_extractor.py) performs a single LLM pass over the cleaned text to extract:
1. `detected_language` (locks the thread's response language on the first turn),
2. `is_pure_skill_task` (whether the request is a focused skill/file lookup rather than an open strategic debate), and
3. Modifiers and actions **strictly allowlisted** to skills configured with `auto_detect: true` (tagged `origin: "inferred"`).

**Examples of Natural-Language Skill Inference:**
- `@Izvor Please keep answers under 80 words and at least 30 words.` → infers `max_words = 80`, `min_words = 30`.
- `@Izvor Only look at the last 5 messages in this thread.` → infers `thread_history` (`history = 5`).
- `@Izvor Bring in Legal Advisor and leave out Sceptic.` → infers `include_agents: ["LegalAdvisor"]` and `exclude_agents: ["Sceptic"]`.
- `@Izvor Look up current Stripe vs. Adyen pricing online.` → infers `web_research` (`query`).
- `@Izvor Check our internal runbooks for the database failover procedure.` → infers `knowledge_base_rag`.
- `@Izvor Summarize the attached report.pdf and save an executive brief to summary.md.` → infers `document_reader` and `document_authoring` (`target_file`).

> [!IMPORTANT]
> **Smart Merge & Security Boundary (`auto_detect: false`)**:
> - **Slash Priority**: If you combine slash commands with prose (e.g., `/max_words 200` alongside *"keep it brief"*), the explicit slash value always wins.
> - **Skill Routing Filter**: For `is_pure_skill_task` prompts, Izvor filters out unequipped personas from the active chain and routes to agents authorized for the inferred skill.
> - **Fail-Closed Privilege Gate**: Loop-control and admin skills (`/rounds`, `/first`, `/open_debate`, `/stop`, `/list_skills`, `/grant_skill`, `/grant_skill_for_all`, `/revoke_skill_for_all`, `/revoke_constraint`, `/forget_thread`) have `auto_detect: false` and **cannot** be triggered via natural language, protecting against prompt-injection escalation.

---

## 7. Skills

Izvor ships with **22 declarative skills** defined via `SKILL.md` manifests: **4 configurable agent tool skills** in [`config/skills/`](config/skills) and **18 built-in orchestrator/prompt-modifier skills** in [`engine/skills/builtin/`](engine/skills/builtin).

### 7.1 Configurable Agent Tool Skills (`config/skills/`)

| Skill | Description | Slash Trigger(s) | Auto-Detect | Notes |
|---|---|---|---|---|
| [`web_research`](config/skills/web_research/SKILL.md) | Searches the live web (`web_search`) and extracts clean text from URLs (`fetch_web_page`). | `/web_research`, `/webresearch` | **Yes** (`true`) | SSRF, DNS-rebinding & Slowloris protected |
| [`knowledge_base_rag`](config/skills/knowledge_base_rag/SKILL.md) | Hybrid LanceDB vector + BM25 search (`search_knowledge_rag`) over `config/rag_knowledge/`. | `/rag`, `/search_docs`, `/knowledge` | **Yes** (`true`) | Supports `all` and `self` scopes |
| [`document_reader`](config/skills/document_reader/SKILL.md) | Lists (`list_documents`) and reads (`read_document`) files in `agents_resources/` or Slack attachments. | `/read_file`, `/read-file`, `/file`, `/attachment`, `/document_reader` | **Yes** (`true`) | `handles_files: true` |
| [`document_authoring`](config/skills/document_authoring/SKILL.md) | Creates or updates non-code documents (`write_document`) inside `agents_resources/`. | `/document_authoring`, `/authoring`, `/write_doc` | **Yes** (`true`) | Requires Slack **HITL** approval (`requires_human_ITL: true`) |

### 7.2 Built-in Orchestrator & Prompt-Modifier Skills (`engine/skills/builtin/`)

| Skill | Description | Slash Trigger(s) | Auto-Detect | Notes |
|---|---|---|---|---|
| [`control_plane`](engine/skills/builtin/control_plane/SKILL.md) | Global structured tools (`nominate_next_speaker`, `request_user_clarification`, `signal_consensus_convergence`). | — *(Global built-in)* | **No** (`false`) | `global: true` for all agents |
| [`max_words`](engine/skills/builtin/max_words/SKILL.md) | Enforces a maximum word count ceiling per agent response (clamped `1–2000`). | `/max_words`, `/max-words`, `/maxwords`, `/max` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`min_words`](engine/skills/builtin/min_words/SKILL.md) | Enforces a minimum word count floor per agent response (clamped `1–1000`). | `/min_words`, `/min-words`, `/minwords`, `/min` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`scribe_max_words`](engine/skills/builtin/scribe_max_words/SKILL.md) | Restricts the maximum word ceiling of the *Discussion Scribe*'s synthesis notes (`1–2000`). | `/scribe_max_words`, `/scribe-max-words`, `/scribemaxwords` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`scribe_min_words`](engine/skills/builtin/scribe_min_words/SKILL.md) | Requires a minimum word count floor for the *Discussion Scribe*'s synthesis notes (`1–2000`). | `/scribe_min_words`, `/scribe-min-words`, `/scribeminwords` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`thread_history`](engine/skills/builtin/thread_history/SKILL.md) | Limits how many recent thread messages are included in turn context (`0–50`). | `/history` | **Yes** (`true`) | Preserves Scribe summary |
| [`include_agents`](engine/skills/builtin/include_agents/SKILL.md) | Adds specific agents into the thread debate pool, overriding prior exclusions. | `/include` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`exclude_agents`](engine/skills/builtin/exclude_agents/SKILL.md) | Excludes specific agents from participating in the thread discussion. | `/exclude` | **Yes** (`true`) | `off` requires **Slack Admin** |
| [`rounds`](engine/skills/builtin/rounds/SKILL.md) | Sets or continues multi-agent debate rounds (`1–5`, global cap `20` turns). | `/rounds`, `/round`, `/next_round`, `/next-round`, `/nextround` | **No** (`false`) | Slash-only |
| [`first`](engine/skills/builtin/first/SKILL.md) | Designates the opening speaker and continues in normal workflow order. | `/first` | **No** (`false`) | Slash-only |
| [`open_debate`](engine/skills/builtin/open_debate/SKILL.md) | Designates the opening speaker and runs an open debate with dynamic peer nominations. | `/open_debate`, `/open-debate`, `/opendebate` | **No** (`false`) | Slash-only |
| [`stop_discussion`](engine/skills/builtin/stop_discussion/SKILL.md) | Immediately halts the active thread debate and chained panel responses. | `/stop` | **No** (`false`) | `terminal: true` |
| [`list_skills`](engine/skills/builtin/list_skills/SKILL.md) | Lists all available platform skills and the agents authorized for each skill. | `/list_skills`, `/list-skills`, `/skills` | **No** (`false`) | Slash-only |
| [`grant_skill`](engine/skills/builtin/grant_skill/SKILL.md) | Grants a specific skill to one or more designated agents for the thread TTL. | `/grant_skill`, `/grant-skill` | **No** (`false`) | **Slack Admin** + Slash-only |
| [`grant_skill_for_all`](engine/skills/builtin/grant_skill_for_all/SKILL.md) | Grants one or more skills to all agents in the current thread for the thread TTL. | `/grant_skill_for_all`, `/grant-skill-for-all`, `/grant_for_all`, `/grant-for-all` | **No** (`false`) | **Slack Admin** + Slash-only |
| [`revoke_skill_for_all`](engine/skills/builtin/revoke_skill_for_all/SKILL.md) | Revokes one or more thread-global skills (or `all`) from all agents in the thread. | `/revoke_skill_for_all`, `/revoke-skill-for-all`, `/revoke_for_all` | **No** (`false`) | **Slack Admin** + Slash-only |
| [`revoke_constraint`](engine/skills/builtin/revoke_constraint/SKILL.md) | Revokes active thread constraints (`max_words`, `min_words`, `scribe_max_words`, `scribe_min_words`, `exclude`, `include`, `language`, `modifiers`, `all`). | `/revoke_constraint`, `/revoke-constraint`, `/revoke` | **No** (`false`) | **Slack Admin** + Slash-only |
| [`forget_thread`](engine/skills/builtin/forget_thread/SKILL.md) | Permanently purges SQLite rows, flat histories, Scribe notes, downloads, and caches for the thread (GDPR erasure). | `/forget_thread`, `/forget-thread` | **No** (`false`) | **Slack Admin** + Slash-only |

---

## 8. Configuration & Customization Quick Guide

All implementer customization lives in **`config/`** and **`agents_resources/`** (leave `engine/` untouched as the immutable core engine). Many settings can also be adjusted live in **Slack App Home**.

> [!TIP]
> For exhaustive field tables, validation rules, reload behavior, and full copy-paste examples, see the **[Configuration & Customization Guide (`docs/getting-started/configuration-guide.md`)](docs/getting-started/configuration-guide.md)**.

### 8.1 Adding & Modifying Debate Agents (`config/settings/agent_settings.json` & `config/personas/`)
- **Persona Prompt (Required):** Create or edit `config/personas/<agent_id>.md` (e.g., [`config/personas/agent-10001.md`](config/personas/agent-10001.md)). Keys must follow `agent-<digits>` (e.g., `agent-10017`).
- **Agent Definition:** Add or update the entry in [`config/settings/agent_settings.json`](config/settings/agent_settings.json) under `"agents"` (`username`, `displayname`, `model`, `icon_url`, `is_short_output`, `skills`) and include its ID in `"enabled_agents"` to join default panel debates.
- **Avatars & Live Edits:** Drop custom PNGs into [`config/avatars/`](config/avatars) (referenced as `"avatars/<file>.png"`), or toggle agents, models, prompts, and skills live via **Slack App Home → Agents Tab**.

### 8.2 Adding & Modifying Skills (`config/skills/`)
- **Create a Skill Directory:** Add `config/skills/<skill_name>/SKILL.md` with YAML frontmatter (`name`, `display_name`, `description`, `scope: agent`, `requires_admin: false|true`, `requires_human_ITL: false|true`, `tools`, and `triggers`) plus Markdown instructions. *(Never reuse built-in skill names from `engine/skills/builtin/`.)*
- **Custom Python Tools:** Add `config/skills/<skill_name>/tools.py` exporting `TOOL_SCHEMAS` (LiteLLM function schemas) and `TOOL_HANDLERS` (`{tool_name: async_or_sync_fn}`).
- **Authorize:** Add `<skill_name>` to an agent's `"skills"` array in [`agent_settings.json`](config/settings/agent_settings.json) or grant per-thread in Slack with `@Izvor /grant_skill <skill_name> /[Agent]`.

### 8.3 Connecting External MCP Servers (`mcp_servers` + `SKILL.md`)
1. **Configure the Server:** Under `"mcp_servers"` in [`config/settings/system_settings.json`](config/settings/system_settings.json), define a `stdio` command (`command`, `args`, `env` with `${ENV_VAR}` expansion from `.env`) or a remote `sse` / `streamable_http` endpoint (`url`, `headers`), plus `mutating_tools` (for Slack HITL approval) and `outbound_pii_masking`.
2. **Bind to a Skill:** Create `config/skills/<skill_name>/SKILL.md` with `mcp_server: <server_key>` and `mcp_tools: ["tool_a", ...]` (or `["*"]`). Tools are auto-namespaced as `mcp__<server>__<tool>`.
3. **Assign & Restart:** Assign the skill to agents in `agent_settings.json` and restart Izvor so startup discovers tools from enabled MCP servers.

### 8.4 Adding & Modifying Workflows (`config/workflows/`)
- **Create or Edit a Preset JSON:** Add `config/workflows/<workflow_id>.json` using `"mode": "fixed"` (`fixed_config.chain`), `"mode": "sandwich"` (`sandwich_config.start_chain` / `end_chain`), or `"mode": "scatter_gather"` (Turn 1 framing + parallel branches + `end_chain`). *(Avoid reserved default IDs: `default_review_flow`, `default_sandwich`, `parallel_dialectic_flow`.)*
- **Activate:** Set `"workflow_config": "<workflow_id>"` in [`agent_settings.json`](config/settings/agent_settings.json), or load/create/modify presets live via **Slack App Home → Workflows Tab**.

### 8.5 Adding & Updating RAG Knowledge Documents (`config/rag_knowledge/`)
- **Folder Scoping:** Put organization-wide docs in [`config/rag_knowledge/shared/`](config/rag_knowledge/shared) (`scope="all"`), and persona-specific docs in `config/rag_knowledge/<agent_id>/` (e.g., [`config/rag_knowledge/agent-10008/`](config/rag_knowledge/agent-10008)) for `scope="self"` and `scope="all"`. Supported formats: `.md`, `.markdown`, `.txt`, `.pdf`.
- **Re-Index Without Restarting:** Files reconcile automatically on startup, or trigger incremental SHA-256 indexing live via:
  ```bash
  curl -X POST -u admin:YourAdminPassword http://localhost:8000/api/rag/reconcile
  ```

### 8.6 Managing Agent Workspace Resources (`agents_resources/`)
- **Direct File Sandbox:** Place `.txt`, `.md`, `.csv`, `.json`, `.pdf`, etc. in [`agents_resources/`](agents_resources) for direct reading (`document_reader`: `list_documents`, `read_document`) and agent-authored deliverables (`document_authoring`: `write_document`, protected by Slack **HITL Approve/Deny**).
- **Visibility:** Files in `agents_resources/` are available immediately with no indexing step (unlike `config/rag_knowledge/`, which is chunked for semantic + BM25 search). Limits (`max_file_size_mb: 50.0`, `max_write_bytes: 1048576`, `workspace_write_blacklist_additional`) are tuned in [`config/settings/system_settings.json`](config/settings/system_settings.json).

---

## 9. Debugging & Troubleshooting

### 1. App starts (`Application Startup Complete`), you trigger it in Slack, and nothing happens in the terminal

If Slack messages produce **zero** `[WEBHOOK]` or `POST /slack/events` log lines in Uvicorn:

1. **Check if Slack disabled Event Subscriptions (`Enable Events` turned Off):**
   Slack automatically turns **Enable Events → Off** if the Request URL repeatedly fails to return `200 OK` (for example, when messages are posted in channels the bot belongs to while `uvicorn` or your tunnel is stopped).
   - Open your app in the [Slack API App Console](https://api.slack.com/apps) → **Event Subscriptions**.
   - Confirm **Enable Events** is **On**, the Request URL (`https://<your-domain>/slack/events`) shows **Verified**, and `app_mention` + `message.channels` (+ `groups`/`im`/`mpim` if used) are subscribed, then click **Save Changes**.
2. **Verify the tunnel reaches FastAPI:**
   - Check locally (`curl http://localhost:8000/health`) and publicly (`curl https://<your-domain>/health`) — both should return `{"status":"healthy","service":"Izvor Multi-Agent Hub"}`.
   - If using `cloudflared`, you can inspect local tunnel metrics at `http://127.0.0.1:20241/metrics` (`cloudflared_tunnel_total_requests`, `cloudflared_tunnel_request_errors`).
   - Remember that ephemeral `trycloudflare.com` or free `ngrok` URLs change on every restart and must be updated in Slack (**Event Subscriptions**, **Interactivity & Shortcuts**) and `.env` (`BASE_AVATAR_URL`).
3. **Check Cloudflare WAF / Bot Fight Mode:**
   If `/health` works from your browser but Slack events never reach `cloudflared`, check **Cloudflare Dashboard → Security → Events** for challenged or blocked `POST` requests to `/slack/events` from Slack's egress IPs.
4. **Verify bot membership, token identity, and trigger format:**
   - Confirm the bot is a member of the channel (`/invite @Izvor`) and that `SLACK_BOT_TOKEN` in `.env` matches the Slack App you are mentioning.
   - In channels, prefix prompts with `@Izvor` (see [Section 6.1](#61-triggering-the-application)). Messages starting with `/` at character 0 are intercepted locally by Slack as slash commands (`"... is not a valid command"`).
5. **Running Izvor on multiple computers:**
   Two separate Slack Apps with the same display name (`Izvor`) are isolated by `app_id` and do not disable each other. Never share the same `SLACK_BOT_TOKEN` / `app_id` or `cloudflared` tunnel token across machines, and avoid adding two same-named bots to the same Slack channel (which makes `@Izvor` autocomplete ambiguous and causes messages in that channel to fail delivery for whichever machine is offline).

### 2. `/slack/events` returns `401 Unauthorized` (`Rejected unauthorized request`)

Look in logs for `Slack signature verification failed!`, `SLACK_SIGNING_SECRET is not configured`, or `Slack request timestamp is out of date (> 5 mins)` ([`slack_security.py`](engine/core/slack_security.py)):
- Verify `SLACK_SIGNING_SECRET` in `.env` matches **Basic Information → App Credentials → Signing Secret** for that app.
- Verify your machine's system clock is accurate (Slack rejects signatures older than 5 minutes).

### 3. App Home tabs, HITL `Approve`/`Deny`, or `Continue Sequence` buttons fail

Interactive components post to `/slack/interactivity` ([`interactivity.py`](engine/adapters/slack/interactivity.py)), not `/slack/events`:
- Confirm **Interactivity & Shortcuts** is **On** and its Request URL is `https://<your-domain>/slack/interactivity`.
- If admin controls reject your click, verify your Slack Member ID is listed in `SLACK_ADMIN_USER_IDS` in `.env`.

### 4. Turns stall, fall back unexpectedly, or duplicate across workers

- **Spend Circuit Breaker / HITL pause:** Check the thread for **Approve / Deny** or **Continue Sequence** prompts, or inspect budget limits in Slack App Home.
- **Missing or invalid LLM API keys:** Check `.env` for the configured agent models in [`config/settings/agent_settings.json`](config/settings/agent_settings.json) and [`config/settings/system_settings.json`](config/settings/system_settings.json); missing keys trigger fallback to `default_reasoning_model` (or the first available key) with a Slack footer.
- **Multiple Uvicorn workers (`WEB_CONCURRENCY > 1`):** Izvor requires a single worker process (`WEB_CONCURRENCY=1` or default) because active thread tasks and interruption state are coordinated in memory ([`main.py`](main.py)).

### 5. Custom persona avatars do not appear in Slack

Slack fetches avatar URLs server-side using `BASE_AVATAR_URL` + `avatars/...` ([`main.py`](main.py)):
- Ensure `BASE_AVATAR_URL` in `.env` is your **public HTTPS tunnel URL** with a trailing slash (e.g., `https://<your-domain>/`), not `http://localhost:8000/`.
- Verify `curl -I https://<your-domain>/avatars/image_43.png` returns `200 OK`.

### 6. Admin `/api/*` endpoints return `500` or `401`

- If logs show `[SECURITY] ADMIN_PASSWORD is not configured in environment/.env`, set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` and pass HTTP Basic Auth (`curl -u admin:<password> http://localhost:8000/api/health`, or GDPR erasure via `curl -X DELETE -u admin:<password> http://localhost:8000/api/threads/<channel_id>/<thread_ts>`).

### 7. Startup or periodic `AUDIT_HMAC_SECRET` / audit integrity warnings

- [`AppIntegrityValidator`](engine/orchestration/integrity_checks.py) verifies the cryptographic hash chain of `data/audit/audit_*.jsonl` on startup and periodically. For zero-downtime key rotation without breaking historical logs, configure a multi-key keyring via `AUDIT_HMAC_KEYS_JSON` and `AUDIT_HMAC_ACTIVE_KEY_ID` in `.env` (each record stores its signing `key_id`, verified in constant time via `hmac.compare_digest()`). If using a single static `AUDIT_HMAC_SECRET`, keep the secret consistent for a given `data/audit/` trail or archive prior logs before rotating.

### 8. Startup `TypeError: unsupported operand type(s) for |: 'type' and 'str'` on Python 3.11–3.13

- Because Python 3.14+ defers annotation evaluation by default (PEP 649), union annotations mixing `|` with forward-referenced string literals (e.g., `str | "MyClass"`) silently succeed on Python 3.14+ but crash eagerly on import/startup on Python **3.11, 3.12, and 3.13**.
- Fix: pull the latest commit (fixed in [`engine/core/models.py`](engine/core/models.py) via `from __future__ import annotations`), or add `from __future__ import annotations` at the top of any new module using `|` with string annotations. Run `pytest tests/unit/test_code_hygiene.py` (`test_no_eager_string_union_annotations_without_future`) to catch regressions before deploying to Python 3.11–3.13. *(Note: `requires-python` targets `>=3.11`, with Python `3.14+` as the primary verified runtime.)*

---

## 10. Documentation Hub

See the [Documentation Hub (`docs/README.md`)](docs/README.md) and the [Comprehensive System Specification](docs/architecture/Izvor_System_Architecture_Comprehensive.md) for deep-dive references:

- **Getting Started**: [Platform Overview](docs/getting-started/overview.md) · [Installation & Cloudflare Tunnel Guide](docs/getting-started/installation.md) · [Configuration & Customization Guide](docs/getting-started/configuration-guide.md) · [Slack App Setup Guide](docs/getting-started/slack-app-setup.md)
- **User Guide**: [Interacting in Slack](docs/user-guide/interacting-in-slack.md) · [Directives & Slash Syntax](docs/user-guide/directives-and-slash-syntax.md) · [Thread Notes & Exports](docs/user-guide/thread-notes-and-exports.md)
- **Architecture & Engine**: [Comprehensive System Specification](docs/architecture/Izvor_System_Architecture_Comprehensive.md) · [System Design](docs/architecture/system-design.md) · [Orchestration Pipeline](docs/architecture/orchestration-pipeline.md) · [Topologies & Routing](docs/architecture/topologies-and-routing.md) · [Thread State & Memory](docs/architecture/thread-state-memory.md) · [Spend Governance](docs/architecture/spend-governance.md)
- **Agents, Skills & Tools**: [Personas Catalog](docs/agents-and-skills/personas-catalog.md) · [Skills Framework](docs/agents-and-skills/skills-framework.md) · [Built-in Skills Reference](docs/agents-and-skills/built-in-skills-reference.md) · [MCP & Tools](docs/agents-and-skills/mcp-and-tools.md)
- **Administration & Security**: [Slack App Home](docs/administration-and-security/slack-app-home.md) · [Security & Auth](docs/administration-and-security/security-and-auth.md) · [Audit Logging Integrity](docs/administration-and-security/audit-logging-integrity.md) · [REST API Reference](docs/administration-and-security/rest-api-reference.md)
- **Development & Testing**: [Test Suite Guide](docs/development-and-testing/test-suite.md) · [E2E Testing Harness](docs/development-and-testing/e2e-testing-harness.md) · [Contributing](docs/development-and-testing/contributing.md)

---

## 11. License

Izvor is source-available under the **Business Source License 1.1 (`BUSL-1.1`)** — see the [LICENSE](LICENSE) file for full terms.

- **Free Use:** Non-production use is permitted free of charge (`Additional Use Grant: None`).
- **Change Date & Change License:** Converts to the **MIT License** on `2029-11-18` (or four years after publication of each version, whichever comes first).
- **Commercial / Production Licensing:** Production or commercial use requires a separate commercial license from the Licensor (Branko Mikusic).

---

## 12. Disclaimer & Limitation of Liability

> [!IMPORTANT]
> This section provides operational and risk notices accompanying this repository. Nothing in this section modifies or supersedes the terms of [LICENSE](LICENSE) (`BUSL-1.1`).

- **Provided "As Is" & Assumption of Risk:** As stated in [LICENSE](LICENSE), Izvor is provided on an **"AS IS"** basis without warranties or conditions of any kind, express or implied. You download, configure, self-host, and operate this software entirely at your own risk. To the maximum extent permitted by applicable law, the author/Licensor shall not be liable for any direct, indirect, incidental, special, consequential, or exemplary damages — including loss of data, security incidents, third-party API/compute charges, or business interruption — arising out of or relating to the use, inability to use, or misuse of this software.
- **Non-Production Use Only (BSL 1.1):** Free use under `BUSL-1.1` (`Additional Use Grant: None`) is strictly limited to non-production purposes. Any production or commercial deployment requires a separate commercial license agreement with its own contractual terms.
- **AI-Generated Content Is Not Professional Advice:** Izvor orchestrates third-party Large Language Models (LLMs) through personas (including *Legal Advisor*, *Security Architect*, *ROI Strategist*, and others). LLM outputs may contain hallucinations, omissions, or outdated/incorrect information. All outputs are provided for informational and exploratory purposes only and **do not constitute legal, financial, security, medical, or other professional advice**. You are solely responsible for independently verifying all outputs before relying or acting on them.
- **Security, Privacy & Compliance Safeguards Are Best-Effort:** Built-in controls — including Presidio NER/regex PII masking, prompt-injection guardrails, indirect tool-output screening, Human-in-the-Loop (HITL) approvals, HMAC audit logging, and `/forget_thread` erasure — are **best-effort technical measures** and **do not guarantee** complete protection against data leakage, prompt injection, or unauthorized actions, nor do they guarantee compliance with GDPR, HIPAA, SOC 2, or other regulatory frameworks. When self-hosting Izvor, you act as the operator/data controller and are solely responsible for reviewing and governing all data transmitted to Slack, upstream LLM providers, and external MCP servers.
- **Third-Party API Costs, Tools & MCP Integrations:** You supply and manage your own LLM API keys, Slack credentials, network tunnels, RAG documents, and external MCP servers. Built-in Spend Governance estimates and USD circuit breakers are approximate operational safeguards and do not guarantee hard billing limits at upstream providers. You bear sole financial and operational responsibility for all third-party usage fees, rate limits, and actions performed by configured tools or MCP servers.
- **Lawful & Authorized Use:** You are solely responsible for ensuring that your deployment, configuration, and use of Izvor comply with all applicable laws, organizational policies, and third-party provider terms of service. The author/Licensor does not endorse and bears no responsibility for any unauthorized or unlawful use by third parties.


