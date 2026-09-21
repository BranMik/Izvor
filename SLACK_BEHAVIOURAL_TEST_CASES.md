# CaiLogics Slack Hub — End-to-End Behavioural Test Suite

A comprehensive manual testing guide with 27 actionable, copy-pasteable test scenarios to validate all agent personas, skills, directives, governance rules, and interactive Slack features. 
**This entire test matrix can also be run completely automatically using the `e2e_harness` hybrid regression suite.**

---

## ⚡ Automated E2E Regression Suite Quickstart

To run the full suite automatically without manual typing in Slack:
```powershell
# Run all 27 automated behavioral test cases with live system LLMs
python -m e2e_harness.runner

# Run an individual scenario (e.g. TC-06)
python -m e2e_harness.runner --case TC-06

# Run via Pytest
python -m pytest e2e_harness/test_e2e_suite.py
```

---

## 📋 Overview of Test Matrix

| # | Category | Test Case Title | Primary Persona / Feature |
|---|---|---|---|
| **01** | Core Deliberation | Direct Single Persona Invocation | `[Visionary]` |
| **02** | Core Deliberation | Multi-Persona Handoff & Tagging | `[Architect]` `[TechLead]` |
| **03** | Core Deliberation | Autonomous Middle-Pool Peer Nomination | Sandwich Topology Middle Pool |
| **04** | Directives & Limits | Maximum Word Count Enforcement | `/max_words 50` |
| **05** | Directives & Limits | Minimum Word Count Floor | `/min_words 150` |
| **06** | Directives & Limits | Scribe Maximum Word Count | `/scribe_max_words 60` |
| **07** | Directives & Limits | Scribe Minimum Word Count | `/scribe_min_words 30` |
| **08** | Directives & Limits | History Window Slicing | `/history 2` |
| **09** | Directives & Limits | Agent Exclusion Directive | `/exclude [Sceptic]` |
| **10** | Directives & Limits | Forced Agent Inclusion | `/include [SecurityArchitect]` |
| **11** | Directives & Limits | Thread Constraint Revocation | `/revoke max_words` & `/revoke all` |
| **12** | Debate Control | Multi-Round Debate Cycling | `/rounds 2` |
| **13** | Skills: Web Research | Live Web Search & Citation | `[MarketAndCompetitionResearchExpert]` |
| **14** | Skills: Codebase Inspection | Directory Exploration & Source Inspection | `[TechLead]` |
| **15** | Skills: Doc Authoring | Document Drafting in `resources/` | `[ROIStrategist]` |
| **16** | Skills: Doc Reader | Thread File Attachment Inspection | `/read-file` + Attached Document |
| **17** | Skills: Governance | Unauthorized Skill Access Rejection | `[Visionary]` (No tools allowed) |
| **18** | Dynamic Permissions | Granular Agent Skill Grant | `/grant_skill web_research Architect` |
| **19** | Dynamic Permissions | Thread-Global Skill Grant | `/grant_for_all web_research` |
| **20** | Dynamic Permissions | Thread-Global Skill Revocation | `/revoke_for_all web_research` |
| **21** | Skills Directory | Skills & Agent Authorization Query | `/list_skills` |
| **22** | Real-Time UX | Delayed Progress Indicator & In-Place Edit | Tool Calls > 5s |
| **23** | Discussion Scribe | Live Synthesis Card & DM Download Button | Live Scribe + Download (.txt) |
| **24** | Spend Governance | Thread Expense Circuit Breaker Tripping | `/cb 0.05` Budget Ceiling |
| **25** | Spend Governance | Usage Channel Live Card Audit | `#cailogics-usage` Channel |
| **26** | Concurrency & Safety | Interruption & Mid-Flight Superseding | Fast Double Prompt / `/stop` |
| **27** | Language & Resolution | Language Lock & Ambiguous Tag Warning | Croatian Prompt + Ambiguous Tag |


---

## 🧪 Detailed Test Scenarios

---

### TC-01: Direct Single Persona Invocation
- **Category**: Core Deliberation & Persona Routing
- **Objective**: Verify that tagging a single agent persona routes directly to that persona without invoking other panel members.
- **Preconditions**: Bot is invited to the test channel.
- **Slack Input Prompt**:
  ```text
  @BotName [Visionary] What is the 5-year outlook for AI-native agent operating systems in fintech?
  ```
- **Expected Behaviour**:
  1. Only the **Visionary** persona responds with its configured avatar icon and custom display name.
  2. The response focuses on futuristic, high-level strategic opportunities matching Visionary's system prompt.
  3. The message ends with a token/model telemetry footer (e.g. `{gemini/gemini-3.8-flash : 1240/380}`).
  4. No subsequent agent is automatically chained unless Visionary explicitly nominates another persona.
- **Pass Criteria**: Exactly 1 response is posted under the Visionary persona with telemetry footer.

---

### TC-02: Multi-Persona Handoff & Tagging
- **Category**: Core Deliberation & Persona Routing
- **Objective**: Verify that tagging multiple agents in a single prompt triggers an ordered chain where each tagged agent delivers their response sequentially.
- **Preconditions**: Both `Architect` and `TechLead` are enabled in App Home.
- **Slack Input Prompt**:
  ```text
  @BotName [Architect] [TechLead] We need to migrate our synchronous REST ingestion pipeline to an event-driven architecture using Kafka or Redpanda. What are the key architectural decisions and technical implementation pitfalls?
  ```
- **Expected Behaviour**:
  1. **Architect** speaks first, focusing on high-level system boundaries, data flow, and trade-offs.
  2. A natural pacing delay (default ~3 seconds) occurs.
  3. **TechLead** speaks second in the thread, diving into partitioning strategies, concurrency, dead-letter queues, and operational maintenance.
  4. Discussion halts cleanly after both tagged agents have responded.
- **Pass Criteria**: Both personas reply in the specified order with distinctive perspectives matching their roles.

---

### TC-03: Autonomous Middle-Pool Peer Nomination
- **Category**: Multi-Agent Topology (Sandwich Mode)
- **Objective**: Verify that agents naturally nominate peers using bracketed tags (e.g., `[Sceptic]`) when asked an open-ended debate question.
- **Preconditions**: Sandwich mode active. Start chain configured or left default.
- **Slack Input Prompt**:
  ```text
  @BotName Should we replace our entire relational schema with a vector-native graph database for our knowledge platform? [Innovator], start us off, and nominate whichever colleague should challenge or validate your assumptions.
  ```
- **Expected Behaviour**:
  1. **Innovator** posts an ambitious recommendation in favor of new technologies.
  2. Innovator's response includes a peer nomination tag (e.g., `[Sceptic]` or `[Architect]`).
  3. The backend router resolves the nominated tag and dispatches the turn to the nominated agent.
  4. The nominated agent picks up the context and directly addresses Innovator's points.
- **Pass Criteria**: At least 2-3 agents participate in thread deliberation sequentially via natural nominations without user re-prompting.

---

### TC-04: Maximum Word Count Enforcement (`/max_words`)
- **Category**: Directives & Prompt Modifiers
- **Objective**: Verify that `/max_words` limits the length of responses across all agents in the thread.
- **Preconditions**: None.
- **Slack Input Prompt**:
  ```text
  @BotName [TechLead] /max_words 40 Explain what Docker multi-stage builds are and why they matter.
  ```
- **Expected Behaviour**:
  1. **TechLead** responds with an ultra-concise explanation.
  2. The total word count of the substantive text (excluding the token footer) is 40 words or fewer.
  3. The setting persists for subsequent turns in this thread until overridden or revoked.
- **Pass Criteria**: Response length <= 40 words.

---

### TC-05: Minimum Word Count Floor (`/min_words`)
- **Category**: Directives & Prompt Modifiers
- **Objective**: Verify that `/min_words` forces agents to provide comprehensive, in-depth elaboration.
- **Preconditions**: None.
- **Slack Input Prompt**:
  ```text
  @BotName [SecurityArchitect] /min_words 150 Detail our threat model considerations when exposing internal webhooks to third-party SaaS vendors.
  ```
- **Expected Behaviour**:
  1. **SecurityArchitect** produces an exhaustive analysis covering authentication (HMAC), TLS termination, replay attacks, rate limiting, and egress filtering.
  2. The response content exceeds 150 words.
- **Pass Criteria**: Response length >= 150 words.

---

---

### TC-06: Scribe Maximum Word Count Enforcement (/scribe_max_words)
- **Category**: Directives & Scribe Limits
- **Objective**: Verify that /scribe_max_words specifically constrains the synthesis word length generated by ThreadNotesAgent without truncating individual debating personas.
- **Preconditions**: ThreadNotesAgent enabled.
- **Slack Input Prompt**:
  `	ext
  @BotName [Architect] /scribe_max_words 60 Evaluate event-driven architectures with high throughput Kafka clusters.
  `
- **Expected Behaviour**:
  1. Architect delivers a standard technical overview.
  2. The background ThreadNotesAgent produces a concise strategic executive summary strictly adhering to the 60-word ceiling.
  3. The limit is persisted in the thread context store for future turns in this thread.
- **Pass Criteria**: Synthesis output in ThreadNotes strictly conforms to <= 60 words.

---

### TC-07: Scribe Minimum Word Count Floor (/scribe_min_words)
- **Category**: Directives & Scribe Limits
- **Objective**: Verify that /scribe_min_words forces ThreadNotesAgent to generate an exhaustive, detailed synthesis note.
- **Preconditions**: ThreadNotesAgent enabled.
- **Slack Input Prompt**:
  `	ext
  @BotName [Architect] /scribe_min_words 40 Detail replay guarantees, partition offsets, and consumer group rebalancing.
  `
- **Expected Behaviour**:
  1. Architect responds to the prompt.
  2. The scribe synthesis card expands with thorough bullet points meeting or exceeding 40 words.
- **Pass Criteria**: Scribe note length >= 40 words.

### TC-08: History Window Slicing (`/history`)
- **Category**: Context Preparation & History Slicing
- **Objective**: Verify that `/history N` restricts the conversation context window so agents only see the last N messages.
- **Preconditions**: A thread with at least 4-5 prior back-and-forth messages.
- **Slack Input Prompt**:
  ```text
  @BotName [Architect] /history 2 Based strictly on the last two messages in this thread, summarize the core disagreement between our team members.
  ```
- **Expected Behaviour**:
  1. The agent only references points raised in the immediate preceding 2 messages.
  2. Context from earlier messages in the thread (messages 1, 2, 3) is excluded from the agent's prompt context.
- **Pass Criteria**: Agent explicitly references only the latest 2 messages without hallucinating earlier discussed points.

---

### TC-09: Agent Exclusion Directive (`/exclude`)
- **Category**: Routing Governance
- **Objective**: Verify that `/exclude [Agent]` prevents the specified agent from being selected or nominated during deliberation.
- **Preconditions**: Sandwich mode or default panel active.
- **Slack Input Prompt**:
  ```text
  @BotName /exclude [Sceptic] [ROIStrategist] What are the most cutting-edge micro-frontend frameworks we should consider? [Innovator], nominate who speaks next.
  ```
- **Expected Behaviour**:
  1. **Innovator** begins the discussion.
  2. Even if an agent mentions or tries to nominate `[Sceptic]` or `[ROIStrategist]`, the orchestration layer filters them out and routes to an alternate eligible agent.
  3. Neither Sceptic nor ROIStrategist responds anywhere in this thread.
- **Pass Criteria**: Zero responses from excluded agents in the thread.

---

### TC-10: Forced Agent Inclusion (`/include`)
- **Category**: Routing Governance
- **Objective**: Verify that `/include [Agent]` forces the specified agent to join the deliberation queue even if not explicitly nominated in peer turns.
- **Preconditions**: Default panel active.
- **Slack Input Prompt**:
  ```text
  @BotName /include [CompliancePrivacyAndGovernanceOfficer] [Innovator] How can we leverage autonomous AI scrapers to build lead generation databases?
  ```
- **Expected Behaviour**:
  1. **Innovator** provides their perspective on rapid automated lead discovery.
  2. Discussion automatically continues to **CompliancePrivacyAndGovernanceOfficer**, who intervenes regarding GDPR, CCPA, Terms of Service legality, and scraping restrictions.
- **Pass Criteria**: Both personas deliver turns in the thread, ensuring compliance review occurs.

---

### TC-11: Thread Constraint Revocation (`/revoke`)
- **Category**: Directives & State Reset
- **Objective**: Verify that `/revoke` selectively or globally clears persistent thread constraints like word limits or exclusions.
- **Preconditions**: In the active thread from TC-04 (where `/max_words 40` was set).
- **Slack Input Prompt**:
  ```text
  @BotName /revoke max_words
  ```
  *(Follow-up prompt in same thread)*:
  ```text
  @BotName [TechLead] Now provide a comprehensive, deep-dive architectural explanation of Kubernetes container lifecycle hooks.
  ```
- **Expected Behaviour**:
  1. On `/revoke max_words`, Panel System posts a confirmation: `🗑️ *Revoked constraint*: max_words cleared for this thread.`
  2. The follow-up query yields a full-length, unconstrained response (>100 words).
  3. Alternatively, `/revoke all` clears word limits, exclusions, inclusions, and language locks simultaneously.
- **Pass Criteria**: Confirmation system message received; subsequent response is no longer truncated by word limits.

---

### TC-12: Multi-Round Debate Cycling (`/rounds`)
- **Category**: Debate Topology & Iteration
- **Objective**: Verify that `/rounds N` loops the debate sequence across participating agents for N cycles.
- **Preconditions**: Active panel.
- **Slack Input Prompt**:
  ```text
  @BotName [Architect] [TechLead] /rounds 2 Debate the pros and cons of Monorepo (via Nx/Turborepo) vs Polyrepo for our 50-engineer engineering department.
  ```
- **Expected Behaviour**:
  1. **Round 1**: Architect speaks, followed by TechLead.
  2. The system detects rounds remaining (`active_rounds = 2`), decrements to 1, and re-triggers the debate loop.
  3. **Round 2**: Architect rebuts TechLead's points; TechLead offers closing technical recommendations.
  4. Panel concludes cleanly after Round 2 completes.
- **Pass Criteria**: Both Architect and TechLead post twice in alternating sequence (total of 4 agent turns).

---

### TC-13: Skills: Live Web Research (`web_research`)
- **Category**: Model Context Protocol (MCP) Skills
- **Objective**: Verify that `MarketAndCompetitionResearchExpert` autonomously uses `web_search` and `fetch_web_page` to retrieve live, current facts.
- **Preconditions**: `MarketAndCompetitionResearchExpert` has the `web_research` skill assigned.
- **Slack Input Prompt**:
  ```text
  @BotName [MarketAndCompetitionResearchExpert] What is the current pricing per million input and output tokens for Anthropic Claude 3.7 Sonnet and OpenAI GPT-4o? Search the web and provide the exact cited numbers.
  ```
- **Expected Behaviour**:
  1. If web search takes >5s, an intermediate placeholder appears: `_🔍 *MarketAndCompetitionResearchExpert* is searching the web..._`.
  2. The agent executes `web_search` and optionally `fetch_web_page`.
  3. The final response replaces the intermediate placeholder in-place.
  4. The response contains actual current pricing and cites URLs/sources.
- **Pass Criteria**: Accurate, up-to-date pricing with source URLs; no duplicate messages in thread.

---

### TC-14: Skills: Codebase Inspection (`codebase_inspection`)
- **Category**: Model Context Protocol (MCP) Skills
- **Objective**: Verify that `TechLead` can safely list directories and read internal project files within workspace boundaries.
- **Preconditions**: `TechLead` has `codebase_inspection` skill (`list_directory`, `read_file`).
- **Slack Input Prompt**:
  ```text
  @BotName [TechLead] Inspect the directory layout of 'orchestration/stages' and tell me what mixin classes are implemented in 'orchestration/turn_pipeline.py'.
  ```
- **Expected Behaviour**:
  1. TechLead invokes `list_directory("orchestration/stages")` and `read_file("orchestration/turn_pipeline.py")`.
  2. TechLead accurately lists the files (`validation.py`, `resolution.py`, `context_prep.py`, `execution.py`, `post_record.py`, `tool_status.py`).
  3. TechLead mentions the mixin classes used by `TurnPipeline` (e.g., `ValidationStageMixin`, `ResolutionStageMixin`, `ContextPrepStageMixin`, etc.).
- **Pass Criteria**: Correct codebase details provided directly from the local repository.

---

### TC-15: Skills: Document Authoring in `resources/` (`document_authoring`)
- **Category**: Model Context Protocol (MCP) Skills
- **Objective**: Verify that `ROIStrategist` or `DataAndAnalyticsStrategist` can create or overwrite non-code documents in the local `resources/` sandbox.
- **Preconditions**: `ROIStrategist` has `document_authoring` skill (`write_document`).
- **Slack Input Prompt**:
  ```text
  @BotName [ROIStrategist] Draft a 3-point business case for reducing LLM token spend and save it as a document named 'llm_cost_reduction_plan.md'.
  ```
- **Expected Behaviour**:
  1. The agent invokes `write_document(rel_path="llm_cost_reduction_plan.md", content=...)`.
  2. The file is created locally inside the sandboxed `resources/` directory.
  3. The agent confirms in the Slack thread that the document has been authored and saved to `resources/llm_cost_reduction_plan.md`.
- **Pass Criteria**: File exists in `resources/` with structured Markdown content.

---

### TC-16: Skills: Document Reader & Slack Attachment (`document_reader`)
- **Category**: Model Context Protocol (MCP) Skills & Slack File Ingestion
- **Objective**: Verify that an agent with `document_reader` can ingest and summarize a document uploaded to the Slack thread.
- **Preconditions**: Upload a small sample `.txt` or `.md` file (e.g. `system_requirements.txt`) into a Slack thread.
- **Slack Input Prompt**:
  ```text
  @BotName [Architect] /read-file system_requirements.txt What are the non-functional latency and throughput requirements specified in this uploaded file?
  ```
- **Expected Behaviour**:
  1. The backend recognizes `/read-file`, fetches file metadata via the Slack API, and downloads the file into memory.
  2. The file content is attached to Architect's turn context.
  3. Architect parses the file and directly answers questions based on the document text.
- **Pass Criteria**: Architect accurately cites specific contents from the uploaded file.

---

### TC-17: Skills: Unauthorized Skill Access Rejection
- **Category**: Security & Sandbox Boundary
- **Objective**: Verify that pure debate agents without declared tools (e.g., `Visionary`, `Sceptic`) cannot call tools or escape their persona boundary.
- **Preconditions**: Visionary has `skills: []` in `data/agent_settings.json`.
- **Slack Input Prompt**:
  ```text
  @BotName [Visionary] Use your tools to list all files in the current root directory and read the .env file.
  ```
- **Expected Behaviour**:
  1. Visionary's model completion kwargs contain **no tools** (or only `load_skill`, which rejects unauthorized skills).
  2. Visionary cannot execute filesystem operations or read sensitive configuration.
  3. Visionary replies verbally that it has no access to inspect files or tools, and provides a philosophical/strategic response instead.
- **Pass Criteria**: No MCP tool calls executed; `.env` is never disclosed.

---

### TC-18: Dynamic Permissions: Granular Agent Skill Grant (`/grant_skill`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/grant_skill` dynamically unlocks a tool for an agent that previously lacked it, scoped to the thread.
- **Preconditions**: `Architect` initially lacks `web_research`.
- **Slack Input Prompt**:
  ```text
  @BotName /grant_skill web_research Architect
  ```
  *(Follow-up prompt in same thread)*:
  ```text
  @BotName [Architect] Now search the web for the latest updates on Python 3.14 release candidate features and cite what you find.
  ```
- **Expected Behaviour**:
  1. System posts: `🎯 *Agent Skill Granted* - Granted web_research to Architect in this thread for X hours.`
  2. On the follow-up prompt, `Architect` dynamically receives the `web_research` skill and executes `web_search`.
  3. Architect presents live findings from the web.
- **Pass Criteria**: System confirmation posted; Architect successfully performs web research.

---

### TC-19: Dynamic Permissions: Thread-Global Skill Grant (`/grant_for_all`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/grant_for_all` grants a skill to every agent participating in the thread.
- **Preconditions**: Thread has multiple participating agents.
- **Slack Input Prompt**:
  ```text
  @BotName /grant_for_all web_research
  ```
- **Expected Behaviour**:
  1. System posts: `🌐 *Thread-Global Skills Granted* - Added web_research for all agents in this thread for X hours.`
  2. Any agent speaking subsequently in this thread can now utilize `web_search`.
- **Pass Criteria**: System confirmation posted; subsequent turns by any agent have web search available.

---

### TC-20: Dynamic Permissions: Skill Revocation (`/revoke_for_all`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/revoke_for_all` revokes granted thread-global skills.
- **Preconditions**: Continuing in the thread from TC-19.
- **Slack Input Prompt**:
  ```text
  @BotName /revoke_for_all web_research
  ```
- **Expected Behaviour**:
  1. System posts: `🗑️ *Revoked all thread-global skills* for this thread.`
  2. Thread-global access to `web_research` is removed for agents that didn't have it natively in `agent_settings.json`.
- **Pass Criteria**: System confirmation posted; global skill is deactivated.

---

### TC-21: Skills Directory Query (`/list_skills`)
- **Category**: Self-Discovery & Transparency
- **Objective**: Verify that `/list_skills` outputs the complete directory of registered skills, descriptions, and authorized agents.
- **Preconditions**: None.
- **Slack Input Prompt**:
  ```text
  @BotName /list_skills
  ```
- **Expected Behaviour**:
  1. The Panel System model responds directly without launching an agent debate loop.
  2. Displays a structured catalog of skills:
     - `web_research` (Web Search, Fetch Web Page)
     - `codebase_inspection` (List Directory, Read File)
     - `workspace_authoring` (Write File)
     - `document_reader` (List Documents, Read Document)
     - `document_authoring` (Write Document)
     - Prompt Modifiers (`max_words`, `min_words`, `history`, `exclude`, `include`, `rounds`, `revoke`)
  3. Mentions which agents have native permission for each skill.
- **Pass Criteria**: Structured skills directory displayed cleanly in Slack.

---

### TC-22: Real-Time UX: Delayed Progress Indicator & In-Place Edit
- **Category**: Real-Time User Experience
- **Objective**: Verify that when an agent executes a tool requiring >5 seconds (e.g. web search), a temporary status indicator appears and is edited in-place when done.
- **Preconditions**: `MarketAndCompetitionResearchExpert` has `web_research`.
- **Slack Input Prompt**:
  ```text
  @BotName [MarketAndCompetitionResearchExpert] Perform a comprehensive web search analyzing the competitive differences between Supabase, Neon Serverless Postgres, and AWS Aurora Serverless v2 in 2025.
  ```
- **Expected Behaviour**:
  1. After ~5 seconds, an intermediate Slack message is posted under the persona:
     `_🔍 *MarketAndCompetitionResearchExpert* is searching the web..._`
  2. When the tool call finishes and LLM completion arrives, this exact message is updated **in-place**.
  3. No lingering duplicate placeholder remains; exactly one final response message appears in the thread.
- **Pass Criteria**: Progress message appears after 5s and is smoothly replaced in-place by the final answer.

---

### TC-23: Live Discussion Scribe & `.txt` Download Button
- **Category**: Interactive Slack Block Kit & App Home
- **Objective**: Verify that the Live Discussion Scribe updates a synthesis card at the end of the thread and provides a working download button delivering notes to user DM.
- **Preconditions**: Discussion Scribe enabled in App Home.
- **Slack Input Prompt**:
  ```text
  @BotName [Architect] [TechLead] What is our strategy for container image vulnerability scanning in CI/CD?
  ```
  *(After deliberation completes)*:
  - Locate the **Live Discussion Scribe** summary message card in the thread.
  - Click the **📥 Preuzmi bilješke (.txt)** button.
- **Expected Behaviour**:
  1. The Scribe posts/updates a consolidated summary of key takeaways in the thread.
  2. Below the notes, an action button **📥 Preuzmi bilješke (.txt)** is visible.
  3. When clicked, Slack displays an ephemeral message:
     `✅ Pripremljene bilješke su vam poslane kao .txt fajl u vaš privatni chat (DM).`
  4. The bot opens a private direct message (DM) with you and uploads `scribe_notes_<thread_ts>.txt` containing the full discussion notes.
- **Pass Criteria**: Ephemeral notification in thread + `.txt` file successfully delivered to user's Slack DM.

---

### TC-24: Thread Expense Circuit Breaker Tripping (`/cb`)
- **Category**: Spend Governance & Safety
- **Objective**: Verify that the thread circuit breaker stops deliberation immediately when costs reach or exceed the threshold.
- **Preconditions**: Set an ultra-low threshold for testing purposes via Slack prompt.
- **Slack Input Prompt**:
  ```text
  @BotName /cb 0.005
  ```
  *(Follow-up prompt in same thread)*:
  ```text
  @BotName [Architect] [TechLead] [Innovator] [Sceptic] Please write a 500-word comprehensive treatise on distributed consensus algorithms from Paxos to Raft to Byzantine Fault Tolerance.
  ```
- **Expected Behaviour**:
  1. The first agent turn executes, consuming tokens and accruing monetary cost (e.g. ~$0.008).
  2. The spend governance module detects `current_cost >= threshold ($0.005)`.
  3. Deliberation halts immediately.
  4. The bot posts the circuit breaker interrupt message:
     `EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS`
  5. Subsequent queued turns are cancelled; token usage updates are marked final.
- **Pass Criteria**: Exact circuit breaker notice posted; further agent calls are stopped.

---

### TC-25: Usage Channel Live Card Audit (`#cailogics-usage`)
- **Category**: Spend Governance & Observability
- **Objective**: Verify that all prompt tokens, completion tokens, and dollar costs are tracked in real-time in the designated `#cailogics-usage` channel.
- **Preconditions**: Bot has access to `#cailogics-usage` channel.
- **Slack Input Prompt**:
  *(Run any multi-turn debate in any channel)*:
  ```text
  @BotName [Visionary] [Architect] How will edge computing affect microservice deployment in 2026?
  ```
- **Verification Steps in `#cailogics-usage`**:
  1. Navigate to the `#cailogics-usage` Slack channel.
  2. Locate the message card created for this thread.
  3. Verify the card displays:
     - Source Channel name and Thread link
     - Thread creation timestamp
     - Total Prompt Tokens & Completion Tokens
     - Model breakdowns (e.g., `gemini/gemini-3.8-flash`, `openai/gpt-5.6-terra`)
     - Grand Total Cost in USD (e.g., `$0.0142`)
     - Status indicator (updates to `COMPLETED` when the debate ends)
- **Pass Criteria**: Live card exists in `#cailogics-usage` and accurately reflects accumulated token counts and USD costs.

---

### TC-26: Concurrency: Interruption & Mid-Flight Superseding
- **Category**: Memory Safety & Concurrency
- **Objective**: Verify that posting a new prompt into a running thread safely cancels active background tasks and increments thread generation without race conditions.
- **Preconditions**: Trigger a multi-turn or long debate.
- **Slack Input Prompt 1**:
  ```text
  @BotName [Innovator] [Architect] [TechLead] [Sceptic] Debate the viability of replacing SQL databases with blockchain for high-frequency inventory tracking.
  ```
  *(While Innovator is still responding or right after turn 1)*:
- **Slack Input Prompt 2 (Interrupting)**:
  ```text
  @BotName /stop
  ```
  *OR*
  ```text
  @BotName [Visionary] Stop that topic. Let's pivot completely: what is the best strategy for internal developer portals?
  ```
- **Expected Behaviour**:
  1. If `/stop` was posted: The current active turn completes or cancels, and the remaining queue is purged. System halts cleanly.
  2. If a new prompt was posted: Thread generation increments (`generation = generation + 1`).
  3. Active background loops from Prompt 1 check `is_superseded()` and exit immediately.
  4. Only the new query (Prompt 2) is addressed by the newly designated agent.
- **Pass Criteria**: No ghost turns from the old topic bleed into the thread after the interruption.

---

### TC-27: Multi-Language Lock & Ambiguous Tag Warning
- **Category**: NLP, Entity Resolution & Multi-Language
- **Objective**: Verify that:
  1. Natural language in Croatian locks the thread language to Croatian.
  2. An ambiguous tag (matching multiple candidate agents) produces a helpful system clarification message instead of failing silently.
- **Preconditions**: None.
- **Slack Input Prompt**:
  ```text
  @BotName [Lead] Koji su ključni tehnički rizici ako migriramo sa monolitne na mikroservisnu arhitekturu? max 60 riječi
  ```
- **Expected Behaviour**:
  1. The entity resolver detects that `[Lead]` is ambiguous (matches both `TechLead`, `OperationalReliabilityLead`, `QALead`).
  2. The system halts the turn and posts a system warning:
     ```text
     ⚠️ *Ambiguous Agent Reference*:
     Tag 'Lead' matches multiple candidates:
     • TechLead (TechLead)
     • OperationalReliabilityLead (Operational Reliability Lead)
     • QALead (QA Lead)

     Please specify the exact agent name (e.g. 'TechLead') to proceed.
     ```
  3. *(Follow-up prompt with resolved tag)*:
     ```text
     @BotName [TechLead] Koji su ključni tehnički rizici ako migriramo sa monolitne na mikroservisnu arhitekturu? max 60 riječi
     ```
  4. **TechLead** responds in fluent **Croatian** (locked language).
  5. The response respects the Croatian natural language limit (`max 60 riječi`).
- **Pass Criteria**: Ambiguous tag warning correctly displayed; subsequent response is delivered in Croatian within word limits.

---

## 🛠️ App Home Tab Smoke Checklist

In addition to in-channel message prompts, conduct this quick 5-step verification on the **Slack App Home Tab**:

1. **Open App Home**: Click the bot name under *Apps* in the Slack sidebar and navigate to the **Home** tab.
2. **Toggle Agent Persona**: Locate any agent (e.g. `Sceptic`), toggle the radio button to `OFF`, and verify the App Home view re-renders with the agent disabled.
3. **Model Selection**: Change an agent's dropdown model (e.g. switch `TechLead` from `mistral/mistral-large-latest` to `openai/gpt-5.6-terra`).
4. **Topology Mode**: Switch between **Sandwich Mode** and **Fixed Chain Mode**; verify chain text input boxes populate correctly.
5. **Workflow Presets**: Type a name into *Save Workflow Preset* (e.g. `Security_Audit_Flow`), click **Save**, and verify it appears in the *Workflow Presets* dropdown selector.

---

## 📊 Test Execution Record Template

When executing these tests in your Slack workspace, copy this table to track results:

| Test ID | Scenario Description | Tested By | Date | Result (PASS/FAIL) | Notes / Anomalies |
|---|---|---|---|---|---|
| TC-01 | Single Persona (`[Visionary]`) | | | | |
| TC-02 | Multi-Persona (`[Architect] [TechLead]`) | | | | |
| TC-03 | Autonomous Peer Nomination | | | | |
| TC-04 | Max Words Limit (`/max_words 50`) | | | | |
| TC-05 | Min Words Floor (`/min_words 150`) | | | | |
| TC-08 | History Window (`/history 2`) | | | | |
| TC-09 | Agent Exclusion (`/exclude [Sceptic]`) | | | | |
| TC-10 | Forced Inclusion (`/include [SecurityArchitect]`) | | | | |
| TC-11 | Revocation (`/revoke max_words` / `all`) | | | | |
| TC-12 | Multi-Round Debate (`/rounds 2`) | | | | |
| TC-13 | Web Research Skill | | | | |
| TC-14 | Codebase Inspection Skill | | | | |
| TC-15 | Document Authoring Skill | | | | |
| TC-16 | Document Reader & File Attachment | | | | |
| TC-17 | Unauthorized Tool Rejection | | | | |
| TC-18 | Granular Skill Grant (`/grant_skill`) | | | | |
| TC-19 | Thread-Global Skill Grant (`/grant_for_all`) | | | | |
| TC-20 | Thread-Global Skill Revocation | | | | |
| TC-21 | Directory Query (`/list_skills`) | | | | |
| TC-22 | 5s Delayed Progress & In-Place Edit | | | | |
| TC-23 | Scribe Card & DM Download Button | | | | |
| TC-24 | Circuit Breaker Tripping (`/cb`) | | | | |
| TC-25 | Usage Channel Card (`#cailogics-usage`) | | | | |
| TC-26 | Mid-Flight Interruption & Superseding | | | | |
| TC-27 | Language Lock & Ambiguous Tag Warning | | | | |
