# Implementation Plan: Local Multi-Agent Platform in Slack & MCP Integration

This plan outlines the architecture and implementation details for a Python FastAPI backend application that turns a single Slack Bot into a multi-agent team (`CaiLogics Hub`). It uses **LiteLLM** for LLM routing across Gemini and Mistral models with distinct personas, **Slack Events API** with dynamic persona swapping (`chat:write.customize`), and **Model Context Protocol (MCP)** via native Python tools for local file context access.

---

## Approved Configuration & Key Decisions

1. **MCP Architecture**: **Option B (Native Pure Python MCP)**.
   - Built directly in Python using standard tool definitions for filesystem interaction (`read_file`, `write_file`, `list_directory`). No external Node.js dependency required.
2. **Thread History Context**: **Enabled**.
   - Agents will retrieve past messages in the Slack thread (`conversations.replies`) so discussions maintain full conversation history.
3. **Default Agent Routing**: **`Gemini_Architect`**.
   - If `@CaiLogics Hub` is mentioned without a pseudo-tag (e.g. `[Gemini_Visionary]`), the request defaults to `Gemini_Architect`.
4. **Models Configured**:
   - Gemini models: `gemini/gemini-3.5-flash`
   - Mistral models: `mistral/mistral-medium-latest`
5. **Security & API Keys**:
   - API keys and Slack secrets stored securely in `.env` (excluded via `.gitignore`).
   - Signature verification enabled for incoming Slack webhooks (`X-Slack-Signature` & `X-Slack-Request-Timestamp`).

---

## Agent Configuration Matrix

| Agent Name | Model | Temperature | System Role | Slack Username | Icon Emoji |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`Gemini_Visionary`** | `gemini/gemini-3.5-flash` | 0.9 (High) | Creative ideation, imagination, concepts | `CaiLogics Visionary` | `:art:` |
| **`Gemini_Architect`** *(Default)* | `gemini/gemini-3.5-flash` | 0.1 (Low) | Analytical overview, structure, directives | `CaiLogics Architect` | `:triangular_ruler:` |
| **`Mistral_Innovator`** | `mistral/mistral-medium-latest` | 0.8 (High) | Alternative code solutions, creative algorithms | `CaiLogics Innovator` | `:bulb:` |
| **`Mistral_TechLead`** | `mistral/mistral-medium-latest` | 0.0 (Low) | Deterministic code review, logic, reliability | `CaiLogics TechLead` | `:shield:` |

---

## File Structure

```
SlackAgenticPanel/
├── .env                        # Local secrets (API keys, Slack tokens - gitignored)
├── .env.example                # Template for required environment variables
├── .gitignore                  # Prevents committing secrets, venv, or cache
├── requirements.txt            # Python dependencies
├── main.py                     # FastAPI application entrypoint & Slack Webhook handler
├── config.py                   # Agent matrix & environment settings
├── slack_security.py           # HMAC signature verification for Slack requests
├── slack_client.py             # Slack API async client wrapper (dynamic persona & thread history)
├── mcp_tools.py                # Native Python MCP tools (filesystem access)
├── agent_router.py             # Regex tag parser & LiteLLM completion orchestration
└── tests/
    ├── test_security.py        # Unit test for Slack signature verification
    └── test_router.py          # Unit test for pseudo-tag parsing & routing
```

---

## Step-by-Step Implementation Steps

1. **Environment Setup & Configuration**:
   - Create `.env` with user-provided API keys (`GEMINI_API_KEY`, `MISTRAL_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`).
   - Create `.env.example` and `.gitignore`.
   - Create `requirements.txt` (`fastapi`, `uvicorn`, `litellm`, `slack_sdk`, `python-dotenv`, `pydantic`, `pytest`).

2. **Core Modules**:
   - **`config.py`**: Load environment variables securely; define `AGENTS_CONFIG` mapping models, temperatures, usernames, and emojis.
   - **`slack_security.py`**: Implement HMAC SHA256 signature verification according to Slack documentation.
   - **`slack_client.py`**: Async wrapper around `slack_sdk.web.async_client.AsyncWebClient` for posting with custom personas (`chat:write.customize`) and fetching thread history (`conversations.replies`).
   - **`mcp_tools.py`**: Native Python filesystem tools (`read_file`, `write_file`, `list_directory`) in JSON Schema format compatible with LiteLLM tool calls.
   - **`agent_router.py`**: Parse message text for `[Agent_Name]` tags or default to `Gemini_Architect`, format conversation history for LiteLLM, handle MCP tool execution loops, and post the final response to Slack.
   - **`main.py`**: FastAPI app handling `/slack/events` webhook with URL verification challenge, signature check, deduplication/bot filtering, and background task execution.

3. **Verification**:
   - Run unit tests for regex parsing and signature verification.
   - Start FastAPI application and test endpoints.
