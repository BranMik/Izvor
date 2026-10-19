# Test Suite & Quality Assurance

This document details the test suites, testing practices, and quality assurance mechanisms for **Izvor** located in [`tests/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests).

---

## 🧪 Test Suite Overview

Izvor features an extensive test suite consisting of **68 automated test files** and **633 passed automated unit, integration, and security tests**.

```
tests/
├── conftest.py                       # Global fixtures, mock environments & Slack stubs
├── test_admin_auth.py                 # HTTP Basic Auth & timing attack defense tests
├── test_agent_settings_ssot.py        # Settings manager, atomic writes & backups
├── test_agent_skills.py               # Dynamic skill execution & parameter schemas
├── test_audit_logger.py               # Cryptographic hash chaining & sequence validation
├── test_code_hygiene.py               # Code hygiene, dangling imports, formatting
├── test_directive_slash_parser.py     # Regex directive extraction & slash commands
├── test_document_parser.py            # PDF, text & markdown document reading
├── test_domain_state_stores.py        # SQLite discussion & context domain stores
├── test_flat_history.py               # Markdown thread transcript recording
├── test_integrity_checks.py           # Startup and periodic integrity verification
├── test_intent_extractor.py           # NLP conversational directive detection
├── test_mcp.py                        # MCP tool sandbox & directory traversal guards
├── test_model_catalog.py              # Provider resolution & default model loaders
├── test_nomination_enforcement.py      # Peer bracket nomination parsing & entity routing
├── test_phase_p0_gates.py             # Phase P0 sanity and security gates
├── test_phase_p1_privilege_boundaries.py # Phase P1 directive origin & privilege boundaries
├── test_phase_p2_reliability.py       # Phase P2 unicode diacritics & candidate fallbacks
├── test_phase_p3_modernization.py     # Phase P3 pipeline DI & thread cleanup
├── test_pricing_service.py            # Pricing agent research & 7-day disk cache
├── test_router_circuit_breaker.py     # Spend thresholds & debate cutoffs
├── test_router_single_turn.py         # End-to-end single-turn agent execution
├── test_router_multi_turn.py          # End-to-end multi-round deliberations
├── test_slack_admin_auth.py           # Slack App Home server-side admin check tests
├── test_slack_client.py               # Slack WebClient mock interactions
├── test_sqlite_engine.py              # SQLite WAL mode, pragmas & concurrent transactions
├── test_tool_loop_and_updates.py      # 5s delayed progress indicator & in-place edits
└── test_turn_pipeline_di.py           # 5-stage pipeline dependency injection isolation
```

---

## 🏃 Running the Tests

### 1. Windows Protocol (Mandatory Non-Interactive & Unbuffered)
On Windows platforms, always prefix shell and test execution with `cmd /c` and run pytest unbuffered (`-u`) to ensure processes exit cleanly:

```bash
cmd /c .venv\Scripts\python.exe -u -m pytest -v
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
# Run security & authentication tests
cmd /c .venv\Scripts\python.exe -u -m pytest -v tests/test_admin_auth.py tests/test_phase_p1_privilege_boundaries.py tests/test_integrity_checks.py

# Run SQLite state engine & domain store tests
cmd /c .venv\Scripts\python.exe -u -m pytest -v tests/test_sqlite_engine.py tests/test_domain_state_stores.py

# Run orchestration & 5-stage pipeline tests
cmd /c .venv\Scripts\python.exe -u -m pytest -v tests/test_turn_pipeline_di.py tests/test_phase_p3_modernization.py

# Run pricing & spend governance tests
cmd /c .venv\Scripts\python.exe -u -m pytest -v tests/test_pricing_service.py tests/test_router_circuit_breaker.py

# Run code hygiene checks
cmd /c .venv\Scripts\python.exe -u -m pytest -v tests/test_code_hygiene.py
```

---

## 🛡️ Testing Conventions & Principles

1. **Deterministic Mocking**: Real LLM API calls and Slack HTTP calls must never be executed in standard unit tests. Use mocked LiteLLM responses and `conftest.py` stubs.
2. **Generation Divergence Tests**: Concurrency tests must verify that incrementing thread generation aborts ongoing background tasks without dangling coroutines.
3. **Clean Fixture Teardown**: Async tests using `ThreadStateManager` or SQLite connections must cleanly close background worker coroutines and reset database connections upon teardown to prevent unhandled runtime warnings.
4. **Hygiene Asserts**: All source files must conform to [`test_code_hygiene.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_code_hygiene.py) standards, preventing debug print statements, broken links, and dead imports.
