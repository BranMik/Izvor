# Test Suite & Quality Assurance

This document details the test suites, testing practices, and quality assurance mechanisms for **Izvor** located in [`tests/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests).

---

## 🧪 Test Suite Overview

Izvor features an extensive test suite consisting of **88 automated test files** and **717 passed automated unit, integration, architectural, and security tests**.

```
tests/
├── conftest.py                                # Global fixtures, mock environments & Slack stubs
├── test_admin_auth.py                         # HTTP Basic Auth & timing attack defense tests
├── test_agent_settings_ssot.py                # Settings manager, atomic writes & backups
├── test_agent_skills.py                       # Dynamic skill execution & parameter schemas
├── test_architecture_layers.py                # AST import boundary guard (core, nlp, pipeline, services)
├── test_async_llm_enforcement.py              # Enforces async acompletion/aembedding across runtime
├── test_audit_logger.py                       # Cryptographic hash chaining, SQLite anchors & tail truncation
├── test_code_hygiene.py                       # Code hygiene, markdown link verification & brand guards
├── test_db_migrations.py                      # SQLite PRAGMA user_version numbered MIGRATIONS tests
├── test_db_transaction_safety.py              # Nested transaction RuntimeError guard & dead-thread eviction
├── test_directive_slash_parser.py             # Regex directive extraction & slash commands
├── test_discussion_loop.py                    # DiscussionLoop multi-turn routing & continuation tests
├── test_document_parser.py                    # PDF, DOCX, XLSX, PPTX, text & markdown reading
├── test_domain_state_stores.py                # SQLite discussion & context domain stores
├── test_fast_forward_convergence.py           # Convergence threshold & Challenger/Rebuttal fast-forward
├── test_flat_history.py                       # Markdown thread transcript recording
├── test_hierarchical_memory_and_evidence.py   # 3-Tier memory (<episodic_thread_summary>, RoundEvidenceStore)
├── test_hitl_checkpoint_resume.py             # Durable TurnCheckpoint pause/resume & atomic HITL transitions
├── test_integrity_checks.py                   # Startup and periodic integrity verification
├── test_intent_extractor.py                   # Unified pre-flight NLP directive & language detection
├── test_mcp.py                                # MCP tool sandbox & directory traversal guards
├── test_mcp_tools_security.py                 # Windows device names, ADS blocking & max_write_bytes cap
├── test_model_catalog.py                      # Provider resolution & default model loaders
├── test_nomination_enforcement.py             # Peer bracket nomination parsing & entity routing
├── test_phase4_control_plane_and_dialectic.py # control_plane tools & RebuttalAdvocate verdict gate
├── test_phase5_parallel_dialectic.py          # Parallel Dialectic (scatter_gather) & anchor isolation
├── test_phase_p0_gates.py                     # Phase P0 sanity and security gates
├── test_phase_p1_privilege_boundaries.py      # Phase P1 directive origin & Slack Admin privilege boundaries
├── test_phase_p2_reliability.py               # Phase P2 unicode diacritics & candidate fallbacks
├── test_phase_p3_modernization.py             # Phase P3 pipeline DI & thread cleanup
├── test_pii_masker_hardening.py               # Single-pass PII unmasking & async_mask verification
├── test_pricing_service.py                    # litellm.model_cost lookup, price bounds & fail-expensive fallback
├── test_retention_service.py                  # Checkpoint payload redaction, TTL pruning & forget_thread
├── test_revisor_service.py                    # Inline InternalRevisor barrier & timeout tests
├── test_router_circuit_breaker.py             # Unconditional daily budget & per-thread circuit breakers
├── test_router_single_turn.py                 # End-to-end single-turn agent execution
├── test_router_multi_turn.py                  # End-to-end multi-round deliberations
├── test_slack_admin_auth.py                   # Slack App Home server-side admin check tests
├── test_slack_client.py                       # Modular Slack WebClient wrapper & file extension neutralization
├── test_slack_interactivity.py                # ActionSpec whole-payload pre-authorization & modular actions
├── test_spend_governance.py                   # Spend ledger, parallel budget reservations & release
├── test_sqlite_engine.py                      # SQLite WAL mode, pragmas & concurrent transactions
├── test_tool_loop_and_updates.py              # ToolPrincipal auth, 5s delayed status & in-place edits
├── test_turn_checkpoint_store.py              # TurnCheckpointStore CRUD & status lifecycle
├── test_turn_pipeline_di.py                   # 5-stage pipeline dependency injection isolation
├── test_turn_pipeline_hooks.py                # PipelineDeps hooks & telemetry trace recording
├── test_web_tools_security.py                 # PinnedIPTransport DNS rebinding, IPv6/NAT64 & fetch cap
└── test_workspace_adapter_port.py             # Hexagonal SlackWorkspaceAdapter & InMemoryWorkspaceAdapter
```

---

## 🏃 Running the Tests

### 1. Windows Protocol (Mandatory Non-Interactive & Unbuffered)
On Windows platforms, always prefix shell and test execution with `cmd /c` and run pytest unbuffered (`-u`) to ensure processes exit cleanly:

```bash
cmd /c .venv\Scripts\python.exe -u -m pytest
```

### 2. Linux / macOS
```bash
pytest -v
```

### 3. Run with Coverage
```bash
cmd /c .venv\Scripts\python.exe -u -m pytest --cov=. --cov-report=term-missing
```

### 4. Run Specific Subsystems
```bash
# Run security, tool sandboxing & authentication tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/test_admin_auth.py tests/test_web_tools_security.py tests/test_mcp_tools_security.py tests/test_slack_interactivity.py

# Run SQLite migrations, transaction safety & retention tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/test_db_migrations.py tests/test_db_transaction_safety.py tests/test_retention_service.py

# Run orchestration, DiscussionLoop, checkpoints & dialectic tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/test_discussion_loop.py tests/test_hitl_checkpoint_resume.py tests/test_phase4_control_plane_and_dialectic.py tests/test_phase5_parallel_dialectic.py

# Run pricing & spend governance tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/test_pricing_service.py tests/test_spend_governance.py tests/test_router_circuit_breaker.py

# Run code hygiene & architecture layer checks
cmd /c .venv\Scripts\python.exe -u -m pytest tests/test_code_hygiene.py tests/test_architecture_layers.py
```

---

## 🛡️ Testing Conventions & Principles

1. **Deterministic Mocking & `InMemoryWorkspaceAdapter`**: Real LLM API calls and Slack HTTP calls must never be executed in standard unit tests. Use [`InMemoryWorkspaceAdapter`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/workspace_adapter.py), [`PipelineDeps`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/protocols.py), and `conftest.py` stubs.
2. **Generation Divergence Tests**: Concurrency tests must verify that incrementing thread generation aborts ongoing background tasks without dangling coroutines.
3. **Clean Fixture Teardown**: Async tests using `ThreadStateManager`, `TurnTraceRecorder`, or SQLite connections must cleanly close background worker coroutines and reset database connections upon teardown.
4. **Hygiene & Layer Asserts**: All source and documentation files must conform to [`test_code_hygiene.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_code_hygiene.py) and [`test_architecture_layers.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_architecture_layers.py), preventing debug print statements, broken markdown file links, legacy brand strings, and upward layer imports.
