# Test Suite & Quality Assurance

This document details the test suites, testing practices, and quality assurance mechanisms for **Izvor** located in [`tests/`](file:///tests).

---

## 🧪 Test Suite Overview

Izvor organizes its tests into a clean, 3-tier hierarchy:
1. **Unit Tests** ([`tests/unit/`](file:///tests/unit)): **66** fast, isolated component test files verifying models, parsers, policies, security sanitizers, pricing logic, and architectural rules.
2. **Integration Tests** ([`tests/integration/`](file:///tests/integration)): **37** multi-component test files verifying SQLite engines, Turn Pipeline lifecycle, Slack interactivity endpoints, RAG Vector DB, HITL checkpoints, and multi-agent loops (combined default suite: **103 files, 1,025 passing tests**).
3. **E2E Behavioral Simulation Harness** ([`tests/e2e/`](file:///tests/e2e)): 28 comprehensive Slack event, WebClient interception, and dialectical scenario simulations (**8** harness modules, executed explicitly via `test_e2e_suite.py` or `tests.e2e.runner`).

```
tests/
├── conftest.py                                # Global test fixtures, path isolation & stub registries
├── unit/                                      # Tier 1: Fast isolated unit tests & static checkers
│   ├── test_admin_auth.py                     # HTTP Basic Auth & timing attack defense
│   ├── test_agent_settings_ssot.py            # Settings manager, atomic writes & backups
│   ├── test_architecture_layers.py            # AST 5-tier DAG boundary guard (core, config_store, nlp, pipeline, services, orchestration, adapters)
│   ├── test_async_llm_enforcement.py          # Enforces async acompletion/aembedding across runtime
│   ├── test_audit_logger.py                   # Cryptographic hash chaining, HMAC keyring rotation, SQLite anchors & tail truncation
│   ├── test_code_hygiene.py                   # Code hygiene, README/docs markdown link verification & brand guards
│   ├── test_directive_slash_parser.py         # Regex directive extraction & slash commands
│   ├── test_document_parser.py                # PDF, text & markdown parsing (code-fence-aware)
│   ├── test_intent_extractor.py               # Unified pre-flight NLP directive & language detection
│   ├── test_pricing_service.py                # Token cost lookup, price bounds & fail-expensive fallback
│   ├── test_rate_limiter.py                   # Token bucket / rate limiter tests
│   └── ...                                    # (66 unit test files total)
├── integration/                               # Tier 2: Multi-component & lifecycle integration tests
│   ├── test_db_migrations.py                  # SQLite PRAGMA user_version numbered MIGRATIONS tests
│   ├── test_discussion_loop.py                # DiscussionLoop multi-turn routing & continuation tests
│   ├── test_domain_state_stores.py            # SQLite discussion & context domain stores
│   ├── test_fast_forward_convergence.py       # Convergence threshold & Challenger/Arbitrator fast-forward
│   ├── test_hitl_checkpoint_resume.py         # Durable TurnCheckpoint pause/resume & atomic HITL transitions
│   ├── test_integration.py                    # Multi-agent turn integration tests
│   ├── test_local_tools.py                    # Local tool sandbox & directory traversal guards
│   ├── test_local_tools_security.py           # Windows device names, ADS blocking & max_write_bytes cap
│   ├── test_phase4_control_plane_and_dialectic.py # control_plane tools & Arbitrator verdict gate
│   ├── test_phase5_parallel_dialectic.py      # Parallel Dialectic (scatter_gather) & anchor isolation
│   ├── test_rag.py                            # LanceDB vector search & retrieval tests
│   ├── test_slack_client.py                   # Modular Slack WebClient wrapper tests
│   ├── test_slack_interactivity.py            # ActionSpec whole-payload pre-authorization & actions
│   ├── test_sqlite_engine.py                  # SQLite WAL mode, pragmas & concurrent transactions
│   ├── test_turn_pipeline_di.py               # 5-stage pipeline dependency injection isolation
│   ├── test_turn_pipeline_hooks.py            # PipelineDeps hooks & telemetry trace recording
│   ├── test_web_tools_security.py             # PinnedIPTransport DNS rebinding, IPv6/NAT64 & fetch cap
│   └── ...                                    # (37 integration test files total)
└── e2e/                                       # Tier 3: Synthetic Slack event & behavioral harness
    ├── conftest.py                            # E2E CLI parser flags (--mock, --hybrid--mocked-llm)
    ├── hybrid_interceptor.py                  # Intercepts outbound WebClient calls & records assertions
    ├── protocol_client.py                     # Synthesizes authentic HMAC-signed Slack Events API POSTs
    ├── runner.py                              # Standalone CLI test runner
    ├── scenarios.py                           # 28 behavioral scenario specifications
    ├── test_e2e_suite.py                      # Pytest runner wrapper
    ├── workspace_adapter.py                   # In-memory workspace adapter
    └── SLACK_BEHAVIOURAL_TEST_CASES.md        # Comprehensive scenario specifications
```

---

## 🏃 Running the Tests

### 1. Default Regression Suite (Unit & Integration)
By default, running `pytest` automatically executes all tests in `tests/unit/` and `tests/integration/`, while cleanly bypassing `tests/e2e/`:

```bash
cmd /c .venv\Scripts\python.exe -u -m pytest
```

### 2. Running E2E Behavioral Simulation Suite
The behavioral harness can be executed deliberately via pytest or the standalone runner:

```bash
# Via pytest:
cmd /c .venv\Scripts\python.exe -u -m pytest tests/e2e/test_e2e_suite.py -v

# Via CLI runner:
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner --all
cmd /c .venv\Scripts\python.exe -u -m tests.e2e.runner --case TC-01
```

### 3. Run Specific Subsystems
```bash
# Run security, tool sandboxing & authentication tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/unit/test_admin_auth.py tests/integration/test_web_tools_security.py tests/integration/test_local_tools_security.py tests/integration/test_slack_interactivity.py

# Run SQLite migrations, transaction safety & retention tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/integration/test_db_migrations.py tests/integration/test_sqlite_engine.py tests/unit/test_retention_service.py

# Run orchestration, DiscussionLoop, checkpoints & dialectic tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/integration/test_discussion_loop.py tests/integration/test_hitl_checkpoint_resume.py tests/integration/test_phase4_control_plane_and_dialectic.py tests/integration/test_phase5_parallel_dialectic.py

# Run pricing & spend governance tests
cmd /c .venv\Scripts\python.exe -u -m pytest tests/unit/test_pricing_service.py tests/unit/test_spend_governance.py tests/integration/test_router_circuit_breaker.py

# Run code hygiene & architecture layer checks
cmd /c .venv\Scripts\python.exe -u -m pytest tests/unit/test_code_hygiene.py tests/unit/test_architecture_layers.py
```

---

## 🛡️ Testing Conventions & Principles

1. **Deterministic Mocking & `InMemoryWorkspaceAdapter`**: Real LLM API calls and Slack HTTP calls must never be executed in standard unit tests. Use [`InMemoryWorkspaceAdapter`](file:///engine/adapters/slack/workspace_adapter.py), [`PipelineDeps`](file:///engine/orchestration/protocols.py), and `conftest.py` stubs.
2. **Generation Divergence Tests**: Concurrency tests must verify that incrementing thread generation aborts ongoing background tasks without dangling coroutines.
3. **Clean Fixture Teardown**: Async tests using `ThreadStateManager`, `TurnTraceRecorder`, or SQLite connections must cleanly close background worker coroutines and reset database connections upon teardown.
4. **Hygiene & Layer Asserts**: All source and documentation files must conform to [`test_code_hygiene.py`](file:///tests/unit/test_code_hygiene.py) and [`test_architecture_layers.py`](file:///tests/unit/test_architecture_layers.py), preventing debug print statements, broken markdown file links, legacy brand strings, and upward layer imports.
