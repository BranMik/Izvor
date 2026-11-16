# Izvor Slack Hub — End-to-End Behavioural Test Suite

A comprehensive testing guide with **38 behavioural test scenarios** (`TC-01`–`TC-38`) validating agent personas, skills, directives, RBAC governance, InternalRevisor verification, Challenger/Arbitrator convergence, MCP tool integration, HITL checkpoints, and interactive Slack workflows.
**This entire test matrix runs automatically via `tests/e2e/`.**

---

## ⚡ Automated E2E Regression Suite Quickstart

```powershell
# 1. Live LLM Execution (DEFAULT: authentic LLM execution for all agents)
cmd /c .venv\Scripts\python.exe -u -m pytest tests\e2e\test_e2e_suite.py
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner

# 2. Offline Deterministic Simulation (Fast CI testing via pure DI doubles)
cmd /c .venv\Scripts\python.exe -u -m pytest tests\e2e\test_e2e_suite.py --mock
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner --mock
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner --mock --case TC-35

# 3. Hybrid Mocked LLM Simulation (Live Slack dispatch + deterministic mocked LLM)
cmd /c .venv\Scripts\python.exe -u -m pytest tests\e2e\test_e2e_suite.py --hybrid-mocked-llm
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner --live-slack --channel C0123456789 --hybrid-mocked-llm --case TC-01
```

---

## 📋 Overview of Test Matrix

| # | Category | Test Case Title | Primary Persona / Feature |
|---|---|---|---|
| **TC-01** | Core Routing | Direct Single Persona Invocation | `/[Visionary]` |
| **TC-02** | Core Routing | Multi-Persona Sequential Chain | `/[Security Architect]` `/[LegalAdvisor]` |
| **TC-03** | Topology | Autonomous Peer Nomination | `/first [Innovator]` |
| **TC-04** | Directives | Maximum Word Count Limit | `/max_words 40` |
| **TC-05** | Directives | Minimum Word Count Floor | `/min_words 60` |
| **TC-06** | Directives | Scribe Maximum Word Count | `/scribe_max_words 40` |
| **TC-07** | Directives | Scribe Minimum Word Count | `/scribe_min_words 30` |
| **TC-08** | Context | History Window Slicing | `/history 2` |
| **TC-09** | Governance | Agent Exclusion Directive | `/exclude /[Innovator]` |
| **TC-10** | Directives | Constraint Revocation | `/revoke_constraint max_words` (Admin) |
| **TC-11** | Topology | Multi-Round Debate Cycling | `/rounds 2` |
| **TC-12** | Skills | Live Web Research Skill | `web_research` (`MarketAndCompetitionResearchExpert`) |
| **TC-13** | Skills | Document Reading in `agents_resources/` | `document_reader` (`compliance_guidelines.txt`) |
| **TC-14** | Skills | Document Authoring in `agents_resources/` | `document_authoring` (`ROIStrategist`) |
| **TC-15** | Skills | Document Reader (`sys_spec.txt`) | `document_reader` (`SecurityArchitect`) |
| **TC-16** | Security | Unauthorized Skill Access Rejection | `SecurityArchitect` requesting `document_authoring` |
| **TC-17** | Permissions | Granular Agent Skill Grant | `/grant_skill web_research /[Security Architect]` |
| **TC-18** | Permissions | Thread-Global Skill Grant | `/grant_skill_for_all web_research` |
| **TC-19** | Permissions | Thread-Global Skill Revocation | `/revoke_skill_for_all web_research` |
| **TC-20** | Discovery | Skills Directory Query | `/list_skills` |
| **TC-21** | UX | Delayed Progress & In-Place Edit | Intermediate status + `chat.update` |
| **TC-22** | Interactivity | Scribe Download Button (`.txt` in DM) | `download_scribe_notes_txt` |
| **TC-23** | Governance | Expense Circuit Breaker Tripping | Thread cost threshold enforcement |
| **TC-24** | Governance | Usage Channel Token Tracking | `usage_tracker` token accounting |
| **TC-25** | Safety | Stop Directive Concurrency | `/stop` mid-flight cancellation |
| **TC-26** | Concurrency & NLP | Mid-Flight Superseding, Croatian Lock & Tag Halts | `generation + 1`, Croatian detection, unrecognized tag halt |
| **TC-27** | Topology | Default Workflow on Untagged Mention | Default review flow (`Visionary` entry) |
| **TC-28** | Security | Declarative Admin RBAC Gate & Audit | Non-admin denial (`CRITICAL` audit) vs admin grant |
| **TC-29** | Permissions | Combined Grant Directive + Inline Continuation | `/grant_skill_for_all ... /[Agent] <prompt>` |
| **TC-30** | Routing | Skill Routing Filter & Pure-Skill Short-Circuit | `filter_unauthorized_skill_agents` + single username hint |
| **TC-31** | State | Exhausted Queue Recovery & `active_prompt` Tracking | Re-init after empty `remaining_agents`, preserve `initial_prompt` |
| **TC-32** | Verification | `InternalRevisor` Evidence Isolation & Stage 2 | Ground-truth check + Stage 2 confirmation |
| **TC-33** | Topology | `Challenger` & `Arbitrator` Convergence Stress-Test | Consecutive convergence -> `Challenger` -> `Arbitrator` |
| **TC-34** | Interactivity | HITL Clarification Suspend & Resume | `request_user_clarification` pause & user reply resume |
| **TC-35** | MCP & Security | External MCP Bridge & Destructive HITL Gate | `mcp__<server>__<tool>` read + `hitl_approve_tool` |
| **TC-36** | Topology | `ScatterGather` Parallel Topology | Concurrent scatter branches + `end_chain` gather |
| **TC-37** | Directives & Privacy | Tight Word Limits, `output_constraints` & PII `ORG` | `max_words < 20` zero grace, configurable limits, `ORG` preserved |
| **TC-38** | Scribe & Interactivity | Scribe SSOT Empty Guard & DM Failure Feedback | Blank output guard + DM open failure ephemeral error |

---

## 🧪 Notes on Execution & Isolation
- Under `pytest`, [`tests/conftest.py`](file:///tests/conftest.py) and [`tests/e2e/conftest.py`](file:///tests/e2e/conftest.py) isolate SQLite (`db_manager`), settings managers, audit logs, and `local_tools` (`agents_resources/`) inside temporary directories.
- Under standalone CLI (`python -m tests.e2e.runner`), `_standalone_cli_isolation()` provides equivalent temporary isolation automatically.
- Admin-only directives (`/grant_skill`, `/grant_skill_for_all`, `/revoke_skill_for_all`, `/revoke_constraint`) execute under configured test admin `U_ADMIN_TESTER`, while standard user `U_USER_TESTER` verifies fail-closed RBAC rejection (`TC-28`).
