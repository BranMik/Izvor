# Izvor — Multi-Agent Dialectical AI Advisory and Orchestration Platform for Slack

<p align="center">
	<img src="izvor_logo.png" alt="Logo" width="200">
</p>

## 1. System Definition

**Izvor** (`v0.9.0`, Python `>=3.11`) is an asynchronous **Multi-Agent Dialectical Deliberation and Orchestration Platform for Slack** built on FastAPI, LiteLLM, SQLite (WAL mode), LanceDB + Tantivy hybrid RAG, Microsoft Presidio privacy filters, and the Model Context Protocol (MCP).

Instead of relying on a single generic chatbot that is prone to sycophancy or narrow framing, Izvor orchestrates structured, multi-turn discussions among **multiple specialized debate personas** (16 default OOB, such as *Visionary*, *Innovator*, *Sceptic*, *Security Architect*, *ROI Strategist*, *Legal Advisor*, and *Tactician*) and **5 built-in system agents** (*Discussion Scribe*, *Internal Revisor*, *Dialectical Challenger*, *Arbitrator*, and *ListSkills*) directly inside Slack threads. Personas critique one another, dynamically nominate peers, invoke sandboxed tools, and synthesize actionable conclusions under strict cost, privacy, and security guardrails.

---

## 2. Core Features

### 2.1 Multi-Persona Deliberation Topologies
Managed by [workflow_manager.py](engine/orchestration/workflow_manager.py) and [sandwich.py](engine/pipeline/orchestrator/sandwich.py), Izvor supports multiple discussion topologies:
- **Sandwich Mode (`sandwich`)**: Opens with a sequential *Start Chain* for problem framing, transitions into an open *Middle Pool* with autonomous peer nominations (`/[@Agent]` or `nominate_next_speaker`), and concludes with a sequential *End Chain* for synthesis.
- **Parallel Dialectic / Scatter-Gather (`scatter_gather`)**: Executes Turn 1 framing, fans out concurrent specialist branches via `asyncio.TaskGroup` using isolated conversation histories (eliminating sequential anchoring bias) with atomic budget pre-reservations, and gathers all perspectives for final *End Chain* synthesis.
- **Fixed Chain (`fixed`) & Custom Ad-Hoc Flows**: Deterministic ordered sequences from preset workflows or explicit user tags in Slack (e.g., `/[Visionary] /[SecurityArchitect]`).
- **Open Debate (`/open_debate`)**: Starts with a designated speaker and opens the floor to the entire active panel without fixed start/end chains.
- **Adversarial Convergence & Fast-Forward**: Tracks consecutive agreement streaks (`signal_consensus_convergence`). Before allowing premature consensus, Izvor injects a mandatory **Challenger → Arbitrator** stress-test gate; the *Arbitrator* either upholds consensus (**Verdict A**, fast-forwarding to the *End Chain*) or exposes flaws (**Verdict B**, reopening specialist debate).

### 2.2 Context Engineering, Epistemic Integrity & Skills
- **3-Tier Thread Memory**: Combines (1) *Episodic Summaries* maintained by the *Discussion Scribe* ([scribe.py](engine/pipeline/discussion_scribe/scribe.py)), (2) *Shared Round Evidence* deduplicating read-only tool outputs across turns ([evidence_store.py](engine/orchestration/state/evidence_store.py)), and (3) *Sliding-Window History* ([message_builder.py](engine/nlp/message_builder.py)).
- **Two-Stage Internal Revisor**: After every turn, [revisor_service.py](engine/orchestration/revisor_service.py) screens the agent's claims against authoritative ground truth (`<initial_user_prompt>` and `<retrieved_evidence>`) with a fast model and confirms any flagged hallucination with a stronger reasoning model before posting a warning.
- **Declarative Skills & Progressive Disclosure**: Loads a **configurable, extensible agent tool skillset** (4 out-of-the-box in [config/skills/](config/skills): `web_research`, `document_reader`, `document_authoring`, `knowledge_base_rag`), **17 built-in directive/control-plane skills** ([engine/skills/builtin/](engine/skills/builtin)), and external **MCP servers** ([client_manager.py](engine/services/mcp/client_manager.py)). Agents start with `load_skill` and unlock authorized tool schemas on demand, enforced by fail-closed `ToolPrincipal` ACLs.
- **Hybrid Local RAG**: Combines dense vector embeddings in LanceDB with Tantivy BM25 full-text search using Reciprocal Rank Fusion ([service.py](engine/services/rag/service.py)), kept in sync via incremental SHA-256 manifest scanning over shared and agent-scoped documents in [config/rag_knowledge/](config/rag_knowledge) ([sync_engine.py](engine/services/rag/sync_engine.py)) with retrieval-time PII masking and structured citations.

### 2.3 Security, Privacy & Spend Governance
- **Defense-in-Depth Security**: HMAC-SHA256 Slack webhook verification ([slack_security.py](engine/core/slack_security.py)), LanceDB cosine-similarity prompt injection guardrails ([guardrail.py](engine/services/security/guardrail.py)), two-way Presidio + regex PII masking ([pii_masker.py](engine/services/security/pii_masker.py)), DNS-pinned SSRF-safe web fetching ([web_tools.py](engine/services/web_tools.py)), and non-repudiable HMAC-chained monthly JSONL audit logs anchored in SQLite ([audit_logger.py](engine/services/audit_logger.py)).
- **Spend Governance & Durable Checkpointing**: Tracks token and USD spend per turn ([pricing_service.py](engine/services/pricing_service.py)), enforces per-thread and daily USD circuit breakers ([circuit_breaker.py](engine/pipeline/spend_governance/circuit_breaker.py)), and serializes mid-turn `TurnCheckpoint` snapshots ([turn_checkpoint_store.py](engine/services/turn_checkpoint_store.py)) for Human-in-the-Loop (HITL) tool approvals and budget pauses.
- **Slack App Home Control Plane**: Interactive UI ([slack_home/](engine/services/slack_home/__init__.py)) for Slack admins to configure workflows, agent models/skills, system settings, and budgets with rolling JSON backups.

<p align="center">
	<img src="slack_app_home.png" alt="Slack App Home Control Plane" width="1000">
</p>
<p align="center">
	<img src="slack_debate.png" alt="Multi-Agent Slack Debate" width="1000">
</p>

---

## 3. Architecture Overview

Izvor is structured around a **3-Pillar Physical Layout** ([paths.py](engine/core/paths.py)) and a **4-Layer Hexagonal Architecture**:

1. **`config/` (Implementer Configuration Pillar)**: Customizable settings ([agent_settings.json](config/settings/agent_settings.json), [system_settings.json](config/settings/system_settings.json)), 16 persona Markdown prompts (`config/personas/`), agent tool skills (`config/skills/`, 4 OOB), custom workflows (`config/workflows/`), and RAG documents (`config/rag_knowledge/`).
2. **`engine/` (Immutable Core Engine Pillar)**: 134 Python modules across 4 layers:
   - **Layer 1 (`engine/core/`)**: Zero-upward-import foundation (`database.py`, `domain.py`, `models.py`, `paths.py`, `llm_utils.py`, `slack_security.py`), enforced by AST tests in [test_architecture_layers.py](tests/unit/test_architecture_layers.py).
   - **Layer 2 (`engine/pipeline/`, `engine/services/`, `engine/skills/`, `engine/nlp/`, `engine/prompts/`)**: Directive parsing, Sandwich/Scatter-Gather routing, hybrid RAG (`LanceDB` + `Tantivy` BM25), MCP client manager, PII masking, HITL, pricing, and audit logging.
   - **Layer 3 (`engine/orchestration/`, `engine/agents/`)**: `DiscussionLoop`, the 5-stage `TurnPipeline`, `ThreadStateManager`, `RevisorService`, `WorkflowManager`, and background support agents (`ThreadNotesAgent`, `UsageAgent`).
   - **Layer 4 (`engine/adapters/slack/`, [main.py](main.py))**: FastAPI server lifespan, Admin REST API, Slack webhook ingress, and `SlackWorkspaceAdapter`.
3. **`data/` (Runtime & UI State Pillar)**: Embedded SQLite WAL database (`data/izvor.db` with 11 tables), Slack App Home state (`data/state/app_settings.json`), hash-chained audit logs (`data/audit/`), LanceDB vector indices (`data/.rag_cache/`), pricing cache (`data/pricing/`), and rolling backups (`data/backups/`).

---

## 4. Main System Flows

### Diagram 1: End-to-End Request & 5-Stage Turn Pipeline Flow

```mermaid
flowchart TD
    User(["Slack User / Admin"]) -->|"Mentions, Slash Directives, Interactive Actions"| Slack["Slack Platform"]
    Slack -->|"POST /slack/events or /slack/interactivity"| Ingress["FastAPI Ingress & HMAC Guard (main.py, slack_security.py)"]
    Ingress -->|"SQLite Event Dedup & 200 OK"| Router["Agent Router & DiscussionLoop (agent_router.py, discussion_loop.py)"]

    subgraph Pipeline["5-Stage TurnPipeline (turn_pipeline.py)"]
        S1["Stage 1: ValidationStage\nGuardrail check, hybrid directive parsing,\nlanguage lock & candidate resolution"]
        S2["Stage 2: ResolutionStage\nAgent config lookup, generation check &\nSpend Governance circuit breaker"]
        S3["Stage 3: ContextPrepStage\n3-Tier Memory assembly, XML prompt building,\nskill filtering & word constraints"]
        S4["Stage 4: ExecutionStage\nPII masking, LiteLLM completion,\nprogressive load_skill & 60s tool loop"]
        S5["Stage 5: PostRecordStage\nMechanical word cutoff, mrkdwn formatting,\nnomination repair & Slack dispatch"]
        S1 --> S2 --> S3 --> S4 --> S5
    end

    Router --> S1
    S4 <-->|"acompletion / aembedding"| LiteLLM["LiteLLM Multi-Provider Gateway\n(Gemini, Claude, OpenAI, Mistral)"]
    S4 <-->|"ToolPrincipal ACLs"| Tools["Sandboxed Tools, Hybrid RAG & MCP Servers"]
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

## 6. Documentation Hub

See the [Documentation Hub (`docs/README.md`)](docs/README.md) and the [Comprehensive System Specification](docs/architecture/Izvor_System_Architecture_Comprehensive.md) for deep-dive references:

- **Getting Started**: [Platform Overview](docs/getting-started/overview.md) · [Installation & Cloudflare Tunnel Guide](docs/getting-started/installation.md) · [Slack App Setup Guide](docs/getting-started/slack-app-setup.md)
- **User Guide**: [Interacting in Slack](docs/user-guide/interacting-in-slack.md) · [Directives & Slash Syntax](docs/user-guide/directives-and-slash-syntax.md) · [Thread Notes & Exports](docs/user-guide/thread-notes-and-exports.md)
- **Architecture & Engine**: [Comprehensive System Specification](docs/architecture/Izvor_System_Architecture_Comprehensive.md) · [System Design](docs/architecture/system-design.md) · [Orchestration Pipeline](docs/architecture/orchestration-pipeline.md) · [Topologies & Routing](docs/architecture/topologies-and-routing.md) · [Thread State & Memory](docs/architecture/thread-state-memory.md) · [Spend Governance](docs/architecture/spend-governance.md)
- **Agents, Skills & Tools**: [Personas Catalog](docs/agents-and-skills/personas-catalog.md) · [Skills Framework](docs/agents-and-skills/skills-framework.md) · [Built-in Skills Reference](docs/agents-and-skills/built-in-skills-reference.md) · [MCP & Tools](docs/agents-and-skills/mcp-and-tools.md)
- **Administration & Security**: [Slack App Home](docs/administration-and-security/slack-app-home.md) · [Security & Auth](docs/administration-and-security/security-and-auth.md) · [Audit Logging Integrity](docs/administration-and-security/audit-logging-integrity.md) · [REST API Reference](docs/administration-and-security/rest-api-reference.md)
- **Development & Testing**: [Test Suite Guide](docs/development-and-testing/test-suite.md) · [E2E Testing Harness](docs/development-and-testing/e2e-testing-harness.md) · [Contributing](docs/development-and-testing/contributing.md)

---

## 7. License

Izvor is source-available under the **Business Source License 1.1 (`BUSL-1.1`)** — see the [LICENSE](LICENSE) file for full terms.

- **Free Use:** Non-production use is permitted free of charge (`Additional Use Grant: None`).
- **Change Date & Change License:** Converts to the **MIT License** on `2029-11-18` (or four years after publication of each version, whichever comes first).
- **Commercial / Production Licensing:** Production or commercial use requires a separate commercial license from the Licensor (Branko Mikusic).


