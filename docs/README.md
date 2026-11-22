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
│   ├── configuration-guide.md             # Adding/Modifying Agents, Skills, MCP, Workflows, RAG & Resources
│   └── slack-app-setup.md                 # Slack API Manifest, Scopes & Webhook Setup
├── architecture/                          # Architecture & Core Engine
│   ├── system-design.md                   # Event Lifecycle, FastAPI Lifespan & Heartbeats
│   ├── orchestration-pipeline.md          # 5-Stage Turn Pipeline, DiscussionLoop & Checkpoints
│   ├── topologies-and-routing.md          # Sandwich, Fixed, Parallel Dialectic & Arbitrator Gate
│   ├── thread-state-memory.md             # 11 SQLite Tables, 3-Tier Memory & Data Retention
│   └── spend-governance.md                # Pricing Service, Token Tracking & Circuit Breakers
├── agents-and-skills/                     # Personas, Prompts & Tools
│   ├── personas-catalog.md                # 17 Persona Roster, Special Roles & Prompt Construction
│   ├── skills-framework.md                # Dynamic Skills Engine, Grant/Revoke & Injection
│   ├── built-in-skills-reference.md       # Complete Manual of 21 Built-in Skills & Directives
│   └── mcp-and-tools.md                   # MCP Workspace Isolation, ToolPrincipal & Web Security
├── administration-and-security/           # Security, Operations & Governance
│   ├── slack-app-home.md                  # App Home UI Configuration, Settings SSOT & Backups
│   ├── security-and-auth.md               # Slack HMAC Verification, Admin Gates & HITL Atomicity
│   ├── audit-logging-integrity.md         # Non-Repudiable Chained Audit Logs & SQLite Anchors
│   └── rest-api-reference.md              # REST Endpoints (/health, /api/skills, /api/rag/*, /api/audit/*)
├── user-guide/                            # End-User Manual & Slack Interactions
│   ├── interacting-in-slack.md            # Mentioning Personas, Thread Discussions & Interruption
│   ├── directives-and-slash-syntax.md     # Bracketed Directives, Word Limits & Rounds
│   └── thread-notes-and-exports.md        # Flat Histories, Scribe Summaries & Exports
└── development-and-testing/               # Developer Guide & Testing
    ├── e2e-testing-harness.md             # Hybrid Interceptor, Protocol Client & Scenarios
    ├── test-suite.md                      # Running the 88 Test Suites (717 Tests), Hygiene & Mocks
    └── contributing.md                    # Engineering Guidelines, Policies & Code Standards
```

---

## 🚀 Quick Links

- **New to Izvor?** Check the [Platform Overview](getting-started/overview.md), [Installation Guide](getting-started/installation.md), and [Configuration & Customization Guide](getting-started/configuration-guide.md).
- **Connecting Slack?** Follow the [Slack App Setup Guide](getting-started/slack-app-setup.md).
- **Exploring Personas?** Browse the [Personas Catalog](agents-and-skills/personas-catalog.md) and [Built-in Skills Reference](agents-and-skills/built-in-skills-reference.md).
- **Understanding Orchestration?** Read [System Design](architecture/system-design.md) and [Topologies & Routing](architecture/topologies-and-routing.md).
- **Securing Your Deployment?** Review [Security & Authentication](administration-and-security/security-and-auth.md) and [Audit Logging Integrity](administration-and-security/audit-logging-integrity.md).
- **Running Tests?** See the [Test Suite Guide](development-and-testing/test-suite.md) and [E2E Testing Harness](development-and-testing/e2e-testing-harness.md).
