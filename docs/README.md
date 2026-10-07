# Izvor Documentation Hub

Welcome to the official documentation for **Izvor** (Multi-Agent Deliberation Hub & Slack Platform).

Izvor is an enterprise-grade, asynchronous multi-agent deliberation platform that connects Slack workspaces (via the Slack Events API, Interactive Components, and App Home) to specialized LLM personas using LiteLLM and Model Context Protocol (MCP) tools.

---

## 📚 Documentation Navigation

```
docs/
├── README.md                              # This Documentation Hub & Index
├── getting-started/                       # Getting Started Guides
│   ├── overview.md                        # Platform Vision, Concepts & Core Features
│   ├── installation.md                    # Prerequisites, Virtual Environment & Configuration
│   └── slack-app-setup.md                 # Slack API Manifest, Scopes & Webhook Setup
├── architecture/                          # Architecture & Core Engine
│   ├── system-design.md                   # Event Lifecycle, FastAPI Lifespan & Heartbeats
│   ├── orchestration-pipeline.md          # 5-Stage Turn Pipeline, DI Pattern & Loop Handlers
│   ├── topologies-and-routing.md          # Sandwich Mode vs. Fixed Mode & Nomination
│   ├── thread-state-memory.md             # ThreadStateManager, TTL Pruning & Superseding
│   └── spend-governance.md                # Pricing Service, Token Tracking & Circuit Breakers
├── agents-and-skills/                     # Personas, Prompts & Tools
│   ├── personas-catalog.md                # Persona Roster, Prompt Construction & Personas SSOT
│   ├── skills-framework.md                # Dynamic Skills Engine, Grant/Revoke & Injection
│   ├── built-in-skills-reference.md       # Complete Manual of 18 Built-in Skills & Directives
│   └── mcp-and-tools.md                   # MCP Workspace Isolation & 5s In-Place Indicators
├── administration-and-security/           # Security, Operations & Governance
│   ├── slack-app-home.md                  # App Home UI Configuration, Settings SSOT & Backups
│   ├── security-and-auth.md               # Slack HMAC Verification & Admin Basic Auth
│   ├── audit-logging-integrity.md         # Non-Repudiable Chained Audit Logs & Integrity Guards
│   └── rest-api-reference.md              # REST Endpoints (/health, /api/skills, /api/rag/*, /api/audit/*)
├── user-guide/                            # End-User Manual & Slack Interactions
│   ├── interacting-in-slack.md            # Mentioning Personas, Thread Discussions & Interruption
│   ├── directives-and-slash-syntax.md     # Bracketed Directives, Word Limits & Rounds
│   └── thread-notes-and-exports.md        # Flat Histories, Scribe Summaries & Exports
└── development-and-testing/               # Developer Guide & Testing
    ├── e2e-testing-harness.md             # Hybrid Interceptor, Protocol Client & Scenarios
    ├── test-suite.md                      # Running the 54+ Test Suites, Hygiene & Mocks
    └── contributing.md                    # Engineering Guidelines, Policies & Code Standards
```

---

## 🚀 Quick Links

- **New to Izvor?** Check the [Platform Overview](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/getting-started/overview.md) and [Installation Guide](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/getting-started/installation.md).
- **Connecting Slack?** Follow the [Slack App Setup Guide](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/getting-started/slack-app-setup.md).
- **Exploring Personas?** Browse the [Personas Catalog](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/agents-and-skills/personas-catalog.md) and [Built-in Skills Reference](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/agents-and-skills/built-in-skills-reference.md).
- **Understanding Orchestration?** Read [System Design](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/architecture/system-design.md) and [Topologies & Routing](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/architecture/topologies-and-routing.md).
- **Securing Your Deployment?** Review [Security & Authentication](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/administration-and-security/security-and-auth.md) and [Audit Logging Integrity](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/administration-and-security/audit-logging-integrity.md).
- **Running Tests?** See the [Test Suite Guide](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/development-and-testing/test-suite.md) and [E2E Testing Harness](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/docs/development-and-testing/e2e-testing-harness.md).
