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
git clone https://github.com/BranMik/SilvanAgora.git izvor
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

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Izvor utilizes:
- `fastapi` and `uvicorn` for the high-performance async server.
- `slack-sdk` for Slack WebClient operations and Block Kit rendering.
- `litellm` for universal multi-provider LLM routing.
- `httpx` for asynchronous HTTP calls and MCP client adapters.
- `pydantic` for data validation.

---

## 🔐 Environment Configuration

Create a `.env` file in the project root by copying [`.env.example`](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/.env.example):

```bash
cp .env.example .env
```

### Configuration Variables Breakdown

Refer to [`core/config.py`](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/core/config.py) for the full definitions:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | **Yes** | `None` | Bot user OAuth token (`xoxb-...`) with write/read scopes. |
| `SLACK_SIGNING_SECRET` | **Yes** | `None` | Slack signing secret for validating HMAC-SHA256 signatures. |
| `OPENROUTER_API_KEY` | Optional | `None` | API key for OpenRouter (recommended universal router). |
| `OPENAI_API_KEY` | Optional | `None` | API key for OpenAI direct access. |
| `ANTHROPIC_API_KEY` | Optional | `None` | API key for Anthropic direct access. |
| `GEMINI_API_KEY` | Optional | `None` | API key for Google Gemini direct access. |
| `MISTRAL_API_KEY` | Optional | `None` | API key for Mistral direct access. |
| `ADMIN_USERNAME` | No | `admin` | HTTP Basic Auth username for protected `/api/*` endpoints. |
| `ADMIN_PASSWORD` | **Yes** (for API) | `None` | HTTP Basic Auth password for protected endpoints. |
| `AUDIT_HMAC_SECRET` | Optional | `None` | Secret key used for signing non-repudiable audit logs. |
| `PORT` | No | `8000` | HTTP port on which FastAPI listens. |
| `MCP_WORKSPACE_DIR` | No | `.` | Root directory to which MCP filesystem tools are confined. |
| `TOOL_CALL_LOOP_TIMEOUT_SECONDS` | No | `60` | Maximum wall-clock execution time for iterative tool calling loops. |
| `DEBATE_TURN_PACING_SECONDS` | No | `3.0` | Delay between consecutive agent turns to prevent flooding. |
| `SLACK_POST_PAUSE_SECONDS` | No | `2.0` | Brief pause before posting to allow human visual parsing. |
| `LOG_LEVEL` | No | `INFO` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

> [!WARNING]
> If `ADMIN_PASSWORD` is left unset, all protected endpoints (`/api/*`) will reject incoming requests with HTTP 500/401 to prevent unauthenticated access. Always set a strong, random password.

---

## 🖥️ Running the Application

### Development Mode

Run the server with Uvicorn auto-reloading:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Windows Proactor Loop Notice

On Windows platforms, Python's Proactor event loop can occasionally stall when no incoming HTTP sockets are active. Izvor automatically starts a background heartbeat coroutine [`keep_loop_alive`](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/main.py#L46-L53) to keep the loop responsive without measurable CPU overhead.

### Health Check Verification

Once running, verify service availability:

```bash
curl http://localhost:8000/health
```

Expected JSON response:
```json
{
  "status": "healthy",
  "service": "CaiLogics Multi-Agent Hub",
  "agents": ["Visionary", "Pragmatist", "Skeptic", "Innovator", "LegalAdvisor", "Security Architect", "Revisor", "Creative Advisor", "Scribe"]
}
```

---

## 🌐 Public Ingress for Slack Webhooks

Because Slack delivers event notifications via HTTP POST to public URLs, your local development server must be reachable from the internet.

Use a tunnel such as `ngrok` or `cloudflared`:

```bash
ngrok http 8000
```

Note your public HTTPS URL (e.g., `https://abcdef123.ngrok-free.app`). You will use this in the [Slack App Setup Guide](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/getting-started/slack-app-setup.md).
