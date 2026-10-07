# Test Suite & Quality Assurance

This document details the test suites, testing practices, and quality assurance mechanisms for **Izvor** located in [`tests/`](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/tests).

---

## 🧪 Test Suite Overview

Izvor features an extensive test suite consisting of **54+ automated test files** and over 200 unit and integration tests.

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
├── test_flat_history.py               # Markdown thread transcript recording
├── test_integrity_checks.py           # Startup and periodic integrity verification
├── test_intent_extractor.py           # NLP conversational directive detection
├── test_mcp.py                        # MCP tool sandbox & directory traversal guards
├── test_model_catalog.py              # Provider resolution & default model loaders
├── test_nomination.py                 # Peer bracket nomination parsing & entity routing
├── test_pricing_service.py            # Pricing agent research & 7-day disk cache
├── test_router_circuit_breaker.py     # Spend thresholds & debate cutoffs
├── test_router_single_turn.py         # End-to-end single-turn agent execution
├── test_router_multi_turn.py          # End-to-end multi-round deliberations
├── test_slack_client.py               # Slack WebClient mock interactions
├── test_tool_loop_and_updates.py      # 5s delayed progress indicator & in-place edits
└── test_turn_pipeline_di.py           # 5-stage pipeline dependency injection isolation
```

---

## 🏃 Running the Tests

### 1. Run the Full Test Suite
```bash
pytest -v
```

### 2. Run with Coverage
```bash
pytest --cov=. --cov-report=term-missing
```

### 3. Run Specific Subsystems
```bash
# Run security & authentication tests
pytest tests/test_security.py tests/test_admin_auth.py tests/test_integrity_checks.py -v

# Run orchestration & pipeline tests
pytest tests/test_turn_pipeline_di.py tests/test_orchestration_chains.py -v

# Run pricing & spend governance tests
pytest tests/test_pricing_service.py tests/test_router_circuit_breaker.py -v

# Run code hygiene checks
pytest tests/test_code_hygiene.py -v
```

---

## 🛡️ Testing Conventions & Principles

1. **Deterministic Mocking**: Real LLM API calls and Slack HTTP calls must never be executed in standard unit tests. Use mocked LiteLLM responses and `conftest.py` stubs.
2. **Generation Divergence Tests**: Concurrency tests must verify that incrementing thread generation aborts ongoing background tasks without dangling coroutines.
3. **Hygiene Asserts**: All new files must conform to [`test_code_hygiene.py`](file:///c:/Users/Hp/Documents/agy2-projects/SilvanAgora/tests/test_code_hygiene.py) standards, preventing debug print statements and dead imports.
