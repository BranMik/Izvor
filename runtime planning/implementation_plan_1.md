# Implementation Plan: Local Multi-Agent Platform in Slack & MCP Integration

This plan outlines the architecture and implementation details for a Python FastAPI backend application that turns a single Slack Bot into a multi-agent team (`CaiLogics Hub`). It uses **LiteLLM** for LLM routing across Gemini and Mistral models with distinct personas, **Slack Events API** with dynamic persona swapping (`chat:write.customize`), and **Model Context Protocol (MCP)** for local file context access.

---

## User Review Required

> [!IMPORTANT]
> **API Keys & Slack App Setup**:
> Before running the app, you will need to set up a `.env` file with:
> - `SLACK_BOT_TOKEN` (starts with `xoxb-`)
> - `SLACK_SIGNING_SECRET`
> - `GEMINI_API_KEY`
> - `MISTRAL_API_KEY`

> [!NOTE]
> Slack requires webhooks to respond within **3 seconds**. The backend will immediately return `HTTP 200 OK` for Slack events and process agent completions asynchronously in the background.

---

## Open Questions for Clarification

Please review the following open questions and provide your preferences:

1. **Thread History Context**:
   - When an agent responds in a Slack thread, should it retrieve past messages in that thread (using `conversations.replies`) as context for LiteLLM?
   - *Default recommendation: Yes, fetching thread history ensures coherent multi-turn discussions between agents and users.*

2. **Default Agent Routing**:
   - If a user mentions `@CaiLogics Hub` without specifying an agent pseudo-tag (e.g. `@CaiLogics Hub How should I structure my project?`), which agent should handle the request?
   - *Options: A) Default to `Gemini_Architect`, B) Default to `Gemini_Visionary`, C) Run a fast classifier router.*

3. **MCP (Model Context Protocol) Scope & Server Type**:
   - The document specifies connecting to an MCP filesystem server (e.g. `@modelcontextprotocol/server-filesystem` via `npx` or a built-in Python MCP filesystem client/server). Should we:
     - **Option A (Standard NPX MCP Filesystem)**: Have the Python backend spawn/connect to `npx -y @modelcontextprotocol/server-filesystem <workspace_dir>` via stdio MCP Client and expose file tools (`read_file`, `list_directory`, `write_file`) to LiteLLM?
     - **Option B (Embedded Python MCP Server/Tools)**: Provide a built-in Python MCP tool handler for local filesystem access inside the backend?
   - *Which local directory path should be exposed via MCP? (e.g., current project directory or configurable via `MCP_WORKSPACE_DIR`?)*

4. **Model Name Formatting for LiteLLM**:
   - `gemini/gemini-2.0-flash` (or `gemini/gemini-1.5-flash`)
   - `mistral/codestral-latest` (or `codestral/codestral-latest`)
   - Are these exact LiteLLM model identifiers aligned with your API provider setup?

---

## Proposed Changes

### Project Structure

We will create a clean modular Python project structure:

```
SlackAgenticPanel/
├── .env.example
├── requirements.txt
├── main.py                     # FastAPI application entrypoint & Slack Webhook handler
├── config.py                   # Agent matrix & environment settings
├── slack_security.py           # HMAC signature verification for Slack requests
├── slack_client.py             # Slack API async client wrapper (dynamic username/emoji)
├── mcp_client.py               # MCP client interface for local workspace tool access
├── agent_router.py             # Regex tag parser & LiteLLM completion orchestration
└── tests/
    └── test_router.py          # Unit tests for tag parsing & security verification
```

---

### Key Components

#### [NEW] [`config.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/config.py)
Defines agent personas, model configurations, temperatures, Slack usernames, and emojis:
- `Gemini_Visionary`: `gemini/gemini-2.0-flash`, temp `0.9`, `:art:`
- `Gemini_Architect`: `gemini/gemini-2.0-flash`, temp `0.1`, `:triangular_ruler:`
- `Mistral_Innovator`: `mistral/codestral-latest`, temp `0.8`, `:bulb:`
- `Mistral_TechLead`: `mistral/codestral-latest`, temp `0.0`, `:shield:`

#### [NEW] [`slack_security.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/slack_security.py)
Implements Slack request verification using `X-Slack-Signature` and `X-Slack-Request-Timestamp` to validate request integrity.

#### [NEW] [`slack_client.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/slack_client.py)
Helper for posting messages back to Slack threads with `chat:write.customize` parameters (`username`, `icon_emoji`, `thread_ts`) and fetching thread reply history.

#### [NEW] [`mcp_client.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/mcp_client.py)
MCP Client integration connecting to the filesystem MCP server. Extracts file contents or context tools to supply to LiteLLM function calls when requested by agents.

#### [NEW] [`agent_router.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/agent_router.py)
Parses incoming Slack message text for pseudo-tags like `[Gemini_Visionary]`, `@Mistral_TechLead`, `[Gemini_Architect]`, etc., constructs full prompt context (including thread history & MCP tools), and calls `litellm.acompletion()`.

#### [NEW] [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/main.py)
FastAPI application defining:
- `POST /slack/events`: Handles `url_verification` challenge, verifies Slack signature, filters out bot loops, triggers background processing for agent responses.
- `GET /health`: Health check endpoint.

---

## Verification Plan

### Automated Tests
1. **Unit tests for Regex & Tag Parsing**: Test extracting tags like `[Gemini_Visionary]`, `@Mistral_TechLead`, `[Gemini_Architect]` from various message formats.
2. **Slack Signature Verification Test**: Test valid and invalid signature verification headers.
3. **LiteLLM Mock Call Test**: Verify completion parameters match agent configuration.

### Manual Verification
1. Run local FastAPI server with `uvicorn main:app --reload --port 8000`.
2. Test Slack `url_verification` request via `curl` or Postman.
3. Verify Cloudflare Tunnel setup (`cloudflared tunnel --url http://localhost:8000`).
4. Perform live test in Slack channel with `@CaiLogics Hub [Gemini_Visionary]` and follow-up `[Mistral_TechLead]`.
