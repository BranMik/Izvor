# Izvor Slack Hub — End-to-End Behavioural Test Suite

A comprehensive manual testing guide with 27 actionable, copy-pasteable test scenarios to validate all agent personas, skills, directives, governance rules, and interactive Slack features. 
**This entire test matrix can also be run completely automatically using the `e2e_testing_harness` hybrid regression suite.**

---

## ⚡ Automated E2E Regression Suite Quickstart

To run the full suite automatically without manual typing in Slack:
```powershell
# 1. Live LLM Execution (DEFAULT: runs authentic LLM completion for all agents)
python -m e2e_testing_harness.runner
python -m e2e_testing_harness.runner --case TC-06
python -m pytest e2e_testing_harness/test_e2e_suite.py

# 2. Hybrid Mocked LLM Simulation (Fast offline testing via deterministic mocks)
python -m e2e_testing_harness.runner --hybrid--mocked-llm
python -m e2e_testing_harness.runner --hybrid--mocked-llm --case TC-06
python -m pytest e2e_testing_harness/test_e2e_suite.py --hybrid--mocked-llm

# 3. Live Slack Dispatch Mode (Streams real cards, avatars & turns to Slack)
# Runs scenario TC-01 into the dedicated channel with 1s pacing (live LLM by default):
python -m e2e_testing_harness.runner --live-slack --channel C0123456789 --case TC-01

# Run with custom testing run prefix (e.g. TEST_RUN_1-[TC-01]):
python -m e2e_testing_harness.runner --live-slack --channel C0123456789 --run-num 1 --case TC-02

# Run live Slack dispatch with hybrid mocked LLM simulation:
python -m e2e_testing_harness.runner --live-slack --channel C0123456789 --hybrid--mocked-llm --case TC-01
```

---

## 📋 Overview of Test Matrix

| # | Category | Test Case Title | Primary Persona / Feature |
|---|---|---|---|
| **01** | Core Deliberation | Direct Single Persona Invocation | `[Visionary]` |
| **02** | Core Deliberation | Multi-Persona Handoff & Tagging | `[Security Architect]` `[LegalAdvisor]` |
| **03** | Core Deliberation | Autonomous Middle-Pool Peer Nomination | Sandwich Topology Middle Pool |
| **04** | Directives & Limits | Maximum Word Count Enforcement | `/max_words 50` |
| **05** | Directives & Limits | Minimum Word Count Floor | `/min_words 150` |
| **06** | Directives & Limits | Scribe Maximum Word Count | `/scribe_max_words 60` |
| **07** | Directives & Limits | Scribe Minimum Word Count | `/scribe_min_words 30` |
| **08** | Directives & Limits | History Window Slicing | `/history 2` |
| **09** | Directives & Limits | Agent Exclusion Directive | `/exclude [Sceptic]` |
| **10** | Directives & Limits | Forced Agent Inclusion | `/include [Security Architect]` |
| **11** | Directives & Limits | Thread Constraint Revocation | `/revoke max_words` & `/revoke all` |
| **12** | Debate Control | Multi-Round Debate Cycling | `/rounds 2` |
| **13** | Skills: Web Research | Live Web Search & Citation | `[MarketAndCompetitionResearchExpert]` |
| **14** | Skills: Codebase Inspection | Directory Exploration & Source Inspection | `[LegalAdvisor]` |
| **15** | Skills: Doc Authoring | Document Drafting in `agents_resources/` | `[ROIStrategist]` |
| **16** | Skills: Doc Reader | Document Reading from `agents_resources/` | `[Security Architect]` |
| **17** | Skills: Governance | Unauthorized Skill Access Rejection | `[Visionary]` (No tools allowed) |
| **18** | Dynamic Permissions | Granular Agent Skill Grant | `/grant_skill web_research [Security Architect]` |
| **19** | Dynamic Permissions | Thread-Global Skill Grant | `/grant_for_all web_research` |
| **20** | Dynamic Permissions | Thread-Global Skill Revocation | `/revoke_for_all web_research` |
| **21** | Skills Directory | Skills & Agent Authorization Query | `/list_skills` |
| **22** | Real-Time UX | Delayed Progress Indicator & In-Place Edit | Tool Calls > 5s |
| **23** | Discussion Scribe | Live Synthesis Card & DM Download Button | Live Scribe + Download (.txt) |
| **24** | Spend Governance | Thread Expense Circuit Breaker Tripping | App Home Settings Threshold |
| **25** | Spend Governance | Usage Channel Live Card Audit | `#izvor-usage` Channel |
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
- **Preconditions**: Both `Security Architect` and `LegalAdvisor` are enabled in App Home.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] [LegalAdvisor] We need to migrate our synchronous REST ingestion pipeline to an event-driven architecture using Kafka or Redpanda. What are the key architectural decisions and technical implementation pitfalls?
  ```
- **Expected Behaviour**:
  1. **Security Architect** speaks first, focusing on high-level system boundaries, data flow, and trade-offs.
  2. A natural pacing delay (default ~3 seconds) occurs.
  3. **LegalAdvisor** speaks second in the thread, diving into partitioning strategies, concurrency, dead-letter queues, and operational maintenance.
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
  2. Innovator's response includes a peer nomination tag (e.g., `[Sceptic]` or `[Security Architect]`).
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
  @BotName [LegalAdvisor] /max_words 40 Explain what Docker multi-stage builds are and why they matter.
  ```
- **Expected Behaviour**:
  1. **LegalAdvisor** responds with an ultra-concise explanation.
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
  @BotName [Security Architect] /min_words 150 Detail our threat model considerations when exposing internal webhooks to third-party SaaS vendors.
  ```
- **Expected Behaviour**:
  1. **Security Architect** produces an exhaustive analysis covering authentication (HMAC), TLS termination, replay attacks, rate limiting, and egress filtering.
  2. The response content exceeds 150 words.
- **Pass Criteria**: Response length >= 150 words.

---

---

### TC-06: Scribe Maximum Word Count Enforcement (`/scribe_max_words`)
- **Category**: Directives & Scribe Limits
- **Objective**: Verify that `/scribe_max_words` specifically constrains the synthesis word length generated by ThreadNotesAgent without truncating individual debating personas.
- **Preconditions**: ThreadNotesAgent enabled.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] /scribe_max_words 60 Evaluate event-driven architectures with high throughput Kafka clusters.
  ```
- **Expected Behaviour**:
  1. Security Architect delivers a standard technical overview.
  2. The background ThreadNotesAgent produces a concise strategic executive summary strictly adhering to the 60-word ceiling.
  3. The limit is persisted in the thread context store for future turns in this thread.
- **Pass Criteria**: Synthesis output in ThreadNotes strictly conforms to <= 60 words.

---

### TC-07: Scribe Minimum Word Count Floor (`/scribe_min_words`)
- **Category**: Directives & Scribe Limits
- **Objective**: Verify that `/scribe_min_words` forces ThreadNotesAgent to generate an exhaustive, detailed synthesis note.
- **Preconditions**: ThreadNotesAgent enabled.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] /scribe_min_words 40 Detail replay guarantees, partition offsets, and consumer group rebalancing.
  ```
- **Expected Behaviour**:
  1. Security Architect responds to the prompt.
  2. The scribe synthesis card expands with thorough bullet points meeting or exceeding 40 words.
- **Pass Criteria**: Scribe note length >= 40 words.

---

### TC-08: History Window Slicing (`/history`)
- **Category**: Context Preparation & History Slicing
- **Objective**: Verify that `/history N` restricts the conversation context window so agents only see the last N messages across multiple prior turns.
- **Preconditions**: A thread populated with 4 prior turns (using `/max_words 10` for token efficiency).
- **Slack Input Prompt**:
  *Prior turns (Turns 1-4)*:
  - Turn 1: `@BotName [LegalAdvisor] /max_words 10 Explain step 1 of database indexing.`
  - Turn 2: `@BotName [Security Architect] /max_words 10 Explain step 2 of sharding.`
  - Turn 3: `@BotName [Security Architect] /max_words 10 Explain step 3 of encryption.`
  - Turn 4: `@BotName [SalesAdvisor] /max_words 10 Explain step 4 of load testing.`
  *Turn 5 (Directive)*:
  ```text
  @BotName /history 2 [Visionary] Based strictly on the last two messages in this thread, summarize our strategy.
  ```
- **Expected Behaviour**:
  1. Turns 1 through 4 establish a multi-turn thread history with concise responses.
  2. On Turn 5, `/history 2` restricts the context history supplied to the model to only the 2 most recent messages (Turns 3 and 4).
  3. Earlier messages (Turns 1 and 2) are pruned from the context window.
- **Pass Criteria**: History directive sets `history_limit=2`, and context prep supplies at most 2 historical messages to the agent.

---

### TC-09: Agent Exclusion Directive (`/exclude`)
- **Category**: Routing Governance
- **Objective**: Verify that `/exclude [Agent]` prevents the specified agent (e.g., `[Innovator]`) from being selected or nominated during deliberation on an untagged default review flow.
- **Preconditions**: Default review flow active (`start_chain`: `["Visionary", "Innovator"]`, `end_chain`: `["Security Architect"]`).
- **Slack Input Prompt**:
  ```text
  @BotName /exclude [Innovator] /max_words 10 Evaluate our distributed consensus and deployment strategy.
  ```
- **Expected Behaviour**:
  1. Because no persona tag is provided, the orchestrator triggers the default workflow.
  2. The entry agent in `start_chain`, **Visionary**, initiates the review.
  3. **Innovator**, who normally follows in `start_chain`, is excluded by the `/exclude [Innovator]` directive and skipped.
  4. Both agents adhere to the `/max_words 10` limit to conserve tokens.
- **Pass Criteria**: `Visionary` responds as the entry agent; `Innovator` does not post anywhere in the thread.
### TC-10: Thread Constraint Revocation (`/revoke_constraint`)
- **Category**: Directives & State Reset
- **Objective**: Verify that `/revoke_constraint` selectively or globally clears persistent thread constraints like word limits or exclusions.
- **Preconditions**: In the active thread from TC-04 (where `/max_words 40` was set).
- **Slack Input Prompt**:
  ```text
  @BotName /revoke_constraint max_words
  ```
  *(Follow-up prompt in same thread)*:
  ```text
  @BotName [LegalAdvisor] Now provide a comprehensive, deep-dive architectural explanation of Kubernetes container lifecycle hooks.
  ```
- **Expected Behaviour**:
  1. On `/revoke_constraint max_words`, Panel System posts a confirmation: `🗑️ *Revoked constraint*: max_words cleared for this thread.`
  2. The follow-up query yields a full-length, unconstrained response (>100 words).
  3. Alternatively, `/revoke_constraint all` clears word limits, exclusions, inclusions, and language locks simultaneously.
- **Pass Criteria**: Confirmation system message received; subsequent response is no longer truncated by word limits.

---

### TC-11: Multi-Round Debate Cycling (`/rounds`)
- **Category**: Debate Topology & Iteration
- **Objective**: Verify that `/rounds N` loops the debate sequence across participating agents for N cycles.
- **Preconditions**: Active panel.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] [LegalAdvisor] /rounds 2 Debate the pros and cons of Monorepo (via Nx/Turborepo) vs Polyrepo for our 50-engineer engineering department.
  ```
- **Expected Behaviour**:
  1. **Round 1**: Security Architect speaks, followed by LegalAdvisor.
  2. The system detects rounds remaining (`active_rounds = 2`), decrements to 1, and re-triggers the debate loop.
  3. **Round 2**: Security Architect rebuts LegalAdvisor's points; LegalAdvisor offers closing technical recommendations.
  4. Panel concludes cleanly after Round 2 completes.
- **Pass Criteria**: Both Security Architect and LegalAdvisor post twice in alternating sequence (total of 4 agent turns).

---

### TC-12: Skills: Live Web Research (`web_research`)
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

### TC-13: Skills: Document Reading in Workspace (`document_reader`)
- **Category**: Model Context Protocol (MCP) Skills
- **Objective**: Verify that `LegalAdvisor` can safely read allowed project and reference files within workspace boundaries.
- **Preconditions**: `LegalAdvisor` has `document_reader` skill (`read_document`).
- **Slack Input Prompt**:
  ```text
  @BotName [LegalAdvisor] Read 'agents_resources/compliance_guidelines.txt' and summarize the key retention obligations.
  ```
- **Expected Behaviour**:
  1. LegalAdvisor invokes `read_document(rel_path="agents_resources/compliance_guidelines.txt")`.
  2. LegalAdvisor accurately parses the text and summarizes the retention guidelines.
- **Pass Criteria**: Correct summary details provided directly from the local document.

---

### TC-14: Skills: Document Authoring in `agents_resources/` (`document_authoring`)
- **Category**: Model Context Protocol (MCP) Skills
- **Objective**: Verify that `ROIStrategist` or `MarketAndCompetitionResearchExpert` can create or overwrite non-code documents in the local `agents_resources/` sandbox.
- **Preconditions**: `ROIStrategist` has `document_authoring` skill (`write_document`).
- **Slack Input Prompt**:
  ```text
  @BotName [ROIStrategist] Draft a 3-point business case for reducing LLM token spend and save it as a document named 'agents_resources/llm_cost_reduction_plan.md'.
  ```
- **Expected Behaviour**:
  1. The agent invokes `write_document(rel_path="agents_resources/llm_cost_reduction_plan.md", content=...)`.
  2. The file is created locally inside the sandboxed `agents_resources/` directory.
  3. The agent confirms in the Slack thread that the document has been authored and saved to `agents_resources/llm_cost_reduction_plan.md`.
- **Pass Criteria**: File exists in `agents_resources/` with structured Markdown content.

---

### TC-15: Skills: Document Reader (`document_reader`)
- **Category**: Model Context Protocol (MCP) Skills & Local Document Ingestion
- **Objective**: Verify that an agent with `document_reader` can read files located in the `agents_resources/` folder.
- **Preconditions**: File `agents_resources/sys_spec.txt` exists on disk with system throughput and latency metrics.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] /read_file agents_resources/sys_spec.txt What is the throughput specified in this file?
  ```
- **Expected Behaviour**:
  1. Security Architect requests `document_reader` via `load_skill`.
  2. Security Architect calls `read_document(rel_path="agents_resources/sys_spec.txt")`.
  3. Security Architect parses the file and answers with the target throughput (`5000 rps`).
- **Pass Criteria**: Security Architect accurately cites `5000 rps` from `agents_resources/sys_spec.txt`.

---

### TC-16: Skills: Unauthorized Skill Access Rejection
- **Category**: Security & Sandbox Boundary
- **Objective**: Verify that attempting to load or execute an unauthorized skill fails with a strict permission denial.
- **Preconditions**: `Security Architect` has `skills: ["document_reader"]` but not `document_authoring`.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] Save this configuration to agents_resources/system_spec.md using document_authoring.
  ```
- **Expected Behaviour**:
  1. Security Architect attempts to invoke `write_document(rel_path="agents_resources/system_spec.md", ...)`.
  2. `skill_loader.execute_tool` evaluates authorization and returns `Permission denied: Agent is not authorized to use tool 'write_document' without skill 'document_authoring'`.
  3. Security Architect reports the authorization denial directly to the user.
- **Pass Criteria**: Permission denial returned from runtime skill registry; unauthorized tool access blocked.

---

### TC-17: Dynamic Permissions: Granular Agent Skill Grant (`/grant_skill`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/grant_skill` dynamically unlocks a tool for an agent that previously lacked it, scoped to the thread.
- **Preconditions**: `Security Architect` initially lacks `web_research`.
- **Slack Input Prompt**:
  ```text
  @BotName /grant_skill web_research [Security Architect]
  ```
  *(Follow-up prompt in same thread)*:
  ```text
  @BotName [Security Architect] Now search the web for the latest updates on Python 3.14 release candidate features and cite what you find.
  ```
- **Expected Behaviour**:
  1. System posts: `🎯 *Agent Skill Granted* - Granted web_research to Security Architect in this thread for X hours.`
  2. Command is strictly exclusive: halts immediately without triggering an agent debate on itself.
  3. On the follow-up prompt, `Security Architect` dynamically receives the `web_research` skill and executes `web_search`.
- **Pass Criteria**: System confirmation posted; command halts cleanly without triggering agent turn.

---

### TC-18: Dynamic Permissions: Thread-Global Skill Grant (`/grant_skill_for_all`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/grant_skill_for_all` grants a skill to every agent participating in the thread.
- **Preconditions**: Thread has multiple participating agents.
- **Slack Input Prompt**:
  ```text
  @BotName /grant_skill_for_all web_research
  ```
- **Expected Behaviour**:
  1. System posts: `🌐 *Thread-Global Skills Granted* - Added web_research for all agents in this thread for X hours.`
  2. Command is strictly exclusive and halts cleanly without launching deliberation on the command itself.
  3. Any agent speaking subsequently in this thread can now utilize `web_search`.
- **Pass Criteria**: System confirmation posted; command halts cleanly; subsequent turns by any agent have web search available.

---

### TC-19: Dynamic Permissions: Skill Revocation (`/revoke_skill_for_all`)
- **Category**: Runtime Permissions
- **Objective**: Verify that `/revoke_skill_for_all` revokes granted thread-global skills.
- **Preconditions**: Continuing in the thread from TC-18.
- **Slack Input Prompt**:
  ```text
  @BotName /revoke_skill_for_all web_research
  ```
- **Expected Behaviour**:
  1. System posts: `🗑️ *Revoked all thread-global skills* for this thread.`
  2. Command is strictly exclusive and halts cleanly without triggering deliberation on itself.
  3. Thread-global access to `web_research` is removed for agents that didn't have it natively in `agent_settings.json`.
- **Pass Criteria**: System confirmation posted; global skill is deactivated; command halts cleanly.

---

### TC-20: Skills Directory Query (`/list_skills`)
- **Category**: Self-Discovery & Transparency
- **Objective**: Verify that `/list_skills` outputs the complete directory of registered skills, descriptions, and authorized agents.
- **Preconditions**: None.
- **Slack Input Prompt**:
  ```text
  @BotName /list_skills
  ```
- **Expected Behaviour**:
  1. The Panel System model responds directly and halts unconditionally without launching an agent debate loop.
  2. Displays a structured catalog of skills:
     - `web_research` (Web Search, Fetch Web Page)
     - `knowledge_base_rag` (Search Knowledge RAG)
     - `control_plane` (Nominate Next Speaker, Request Clarification, Signal Convergence)
     - `document_reader` (List Documents, Read Document)
     - `document_authoring` (Write Document)
     - Control Commands (`/grant_skill`, `/grant_skill_for_all`, `/revoke_constraint`, `/revoke_skill_for_all`, `/list_skills`)
     - Directives (`/max_words`, `/min_words`, `/scribe_max_words`, `/scribe_min_words`, `/history`, `/exclude`, `/include`, `/rounds`)
  3. Mentions which agents have native permission for each skill.
- **Pass Criteria**: Structured skills directory displayed cleanly in Slack; no subsequent agent turns executed.

---

### TC-21: Real-Time UX: Delayed Progress Indicator & In-Place Edit
- **Category**: Real-Time User Experience
- **Objective**: Verify that when an agent executes a tool requiring >5 seconds (e.g. web search), a temporary status indicator appears and is edited in-place when done.
- **Preconditions**: `MarketAndCompetitionResearchExpert` has `web_research`.
- **Slack Input Prompt**:
  ```text
  @BotName [MarketAndCompetitionResearchExpert] Comprehensive web search on vector databases.
  ```
- **Expected Behaviour**:
  1. After ~5 seconds, an intermediate Slack message is posted under the persona:
     `_🔍 *MarketAndCompetitionResearchExpert* is searching the web..._`
  2. When the tool call finishes, this exact message is updated **in-place**.
  3. Response contains real vector database comparative findings (Pinecone, Qdrant, Milvus).
  4. Exactly one final response message appears in the thread without duplicates.
- **Pass Criteria**: Progress message replaced in-place; response contains vector database findings.

---

### TC-22: Live Discussion Scribe & `.txt` Download Button
- **Category**: Interactive Slack Block Kit & App Home
- **Objective**: Verify that the Live Discussion Scribe updates a synthesis card at the end of the thread and provides a working download button delivering notes to user DM.
- **Preconditions**: Discussion Scribe enabled in App Home.
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] [LegalAdvisor] /max_words 25 What is our strategy for container image vulnerability scanning in CI/CD?
  ```
  *(After deliberation completes)*:
  - Locate the **Live Discussion Scribe** summary message card in the thread.
  - Click the **📥 Download Notes (.txt)** button.
- **Expected Behaviour**:
  1. The Scribe posts/updates a consolidated summary of key takeaways in the thread.
  2. Below the notes, an action button **📥 Download Notes (.txt)** is visible in English.
  3. When clicked, Slack displays an ephemeral message in English:
     `✅ Prepared notes have been sent as a .txt file to your direct message (DM).`
  4. The bot opens a private direct message (DM) with the user and uploads `scribe_notes_<thread_ts>.txt`.
- **Pass Criteria**: Ephemeral notification in English + `.txt` file successfully delivered to user's Slack DM.e successfully delivered to user's Slack DM.

---

### TC-23: Thread Expense Circuit Breaker Tripping
- **Category**: Spend Governance & Safety
- **Objective**: Verify that the thread circuit breaker stops deliberation immediately when costs reach or exceed the threshold configured in App Home.
- **Preconditions**: In Slack App Home > Settings, toggle Circuit Breaker to `ON` and set an ultra-low threshold for testing purposes (e.g., `$0.005`).
- **Slack Input Prompt**:
  ```text
  @BotName [Security Architect] [LegalAdvisor] [Innovator] [Sceptic] Please write a 500-word comprehensive treatise on distributed consensus algorithms from Paxos to Raft to Byzantine Fault Tolerance.
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

### TC-24: Usage Channel Live Card Audit (`#izvor-usage`)
- **Category**: Spend Governance & Observability
- **Objective**: Verify that all prompt tokens, completion tokens, and dollar costs are tracked in real-time in the designated `#izvor-usage` channel.
- **Preconditions**: Bot has access to `#izvor-usage` channel.
- **Slack Input Prompt**:
  *(Run any multi-turn debate in any channel)*:
  ```text
  @BotName [Visionary] [Security Architect] How will edge computing affect microservice deployment in 2026?
  ```
- **Verification Steps in `#izvor-usage`**:
  1. Navigate to the `#izvor-usage` Slack channel.
  2. Locate the message card created for this thread.
  3. Verify the card displays:
     - Source Channel name and Thread link
     - Thread creation timestamp
     - Total Prompt Tokens & Completion Tokens
     - Model breakdowns (e.g., `gemini/gemini-3.8-flash`, `openai/gpt-5.6-terra`)
     - Grand Total Cost in USD (e.g., `$0.0142`)
     - Status indicator (updates to `COMPLETED` when the debate ends)
- **Pass Criteria**: Live card exists in `#izvor-usage` and accurately reflects accumulated token counts and USD costs.

---

### TC-25: Concurrency: Interruption & Mid-Flight Superseding
- **Category**: Memory Safety & Concurrency
- **Objective**: Verify that posting a new prompt into a running thread safely cancels active background tasks and increments thread generation without race conditions.
- **Preconditions**: Trigger a multi-turn or long debate.
- **Slack Input Prompt 1**:
  ```text
  @BotName [Innovator] [Security Architect] [LegalAdvisor] [Sceptic] Debate the viability of replacing SQL databases with blockchain for high-frequency inventory tracking.
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

### TC-27: Default Workflow on Untagged Mention
- **Category**: Multi-Agent Topology (Default Sandwich Review Flow)
- **Objective**: Verify that when a user or tester posts a message without explicit agent bracket tags (`@BotName <prompt>`), the system applies the default review workflow (`Visionary` -> `Innovator` -> middle pool -> `Security Architect`).
- **Preconditions**: Standard default settings active (no manual persona tagging).
- **Slack Input Prompt**:
  ```text
  @BotName Evaluate our distributed consensus and deployment strategy for 2026.
  ```
- **Expected Behaviour**:
  1. Because no explicit persona tag is provided, the orchestrator defaults to the standard sandwich review flow.
  2. **Visionary** opens the review as the first agent in `start_chain`.
  3. **Visionary** nominates **Innovator** (`[Innovator]`).
  4. Deliberation proceeds sequentially through the panel and concludes with **Security Architect** (`end_chain`).
- **Pass Criteria**: Conversation starts with `Visionary`, delegates to `Innovator`, and chains through at least 2 distinct agents without manual intervention.

---

## 🛠️ App Home Tab Smoke Checklist

In addition to in-channel message prompts, conduct this quick 5-step verification on the **Slack App Home Tab**:

1. **Open App Home**: Click the bot name under *Apps* in the Slack sidebar and navigate to the **Home** tab.
2. **Toggle Agent Persona**: Locate any agent (e.g. `Sceptic`), toggle the radio button to `OFF`, and verify the App Home view re-renders with the agent disabled.
3. **Model Selection**: Change an agent's dropdown model (e.g. switch `LegalAdvisor` from `mistral/mistral-large-latest` to `openai/gpt-5.6-terra`).
4. **Topology Mode**: Switch between **Sandwich Mode** and **Fixed Chain Mode**; verify chain text input boxes populate correctly.
5. **Workflow Presets**: Type a name into *Save Workflow Preset* (e.g. `Security_Audit_Flow`), click **Save**, and verify it appears in the *Workflow Presets* dropdown selector.

---

## 📊 Test Execution Record Template

When executing these tests in your Slack workspace, copy this table to track results:

| Test ID | Scenario Description | Tested By | Date | Result (PASS/FAIL) | Notes / Anomalies |
|---|---|---|---|---|---|
| TC-01 | Single Persona (`[Visionary]`) | | | | |
| TC-02 | Multi-Persona (`[Security Architect] [LegalAdvisor]`) | | | | |
| TC-03 | Autonomous Peer Nomination | | | | |
| TC-04 | Max Words Limit (`/max_words 50`) | | | | |
| TC-05 | Min Words Floor (`/min_words 150`) | | | | |
| TC-06 | Scribe Max Words (`/scribe_max_words 60`) | | | | |
| TC-07 | Scribe Min Words (`/scribe_min_words 40`) | | | | |
| TC-08 | History Window (`/history 2`) | | | | |
| TC-09 | Agent Exclusion (`/exclude [Innovator]`) | | | | |
| TC-10 | Revocation (`/revoke_constraint max_words` / `all`) | | | | |
| TC-11 | Multi-Round Debate (`/rounds 2`) | | | | |
| TC-12 | Web Research Skill | | | | |
| TC-13 | Codebase Inspection Skill | | | | |
| TC-14 | Document Authoring (`agents_resources/`) | | | | |
| TC-15 | Document Reader (`agents_resources/sys_spec.txt`) | | | | |
| TC-16 | Unauthorized Tool Rejection | | | | |
| TC-17 | Granular Skill Grant (`/grant_skill web_research [Security Architect]`) | | | | |
| TC-18 | Thread-Global Skill Grant (`/grant_skill_for_all`) | | | | |
| TC-19 | Thread-Global Skill Revocation (`/revoke_skill_for_all`) | | | | |
| TC-20 | Directory Query (`/list_skills`) | | | | |
| TC-21 | 5s Delayed Progress & In-Place Edit | | | | |
| TC-22 | Scribe Card & DM Download Button | | | | |
| TC-23 | Circuit Breaker Tripping (App Home Settings) | | | | |
| TC-24 | Usage Channel Card (`#izvor-usage`) | | | | |
| TC-25 | Mid-Flight Interruption & Superseding | | | | |
| TC-27 | Default Workflow on Untagged Mention | | | | |
