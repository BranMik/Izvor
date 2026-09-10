# Deferred Linter Fixes & Evaluation Report

This document records all linting issues that were intentionally **skipped** during the repository-wide lint cleanup, following the directive:
> *"If something is ambiguous or fix could have negative effects, please skip that fix and write description and your evaluation of it in the separate document DEFFERED_LINTER_FIXES.md . Don't fix anything by disabling linter/ruff for the line/error, that is just removal of detection and I would need to check that first and decide on this."*

None of these issues were suppressed with `# noqa` or linter configuration overrides. They remain detectable by the linter so you can review each case and decide on the appropriate architectural resolution.

---

## Summary of Cleanup Progress

- **Initial Linter Issues Found:** 310
- **Issues Cleanly Fixed in Code:** 231 (including all 11 `S110` occurrences resolved via `logger.debug` and dynamic `LOG_LEVEL`)
- **Issues Deferred for User Review:** 79
- **Linters/Rule Suppressions Added:** 0 (`# noqa` was not used anywhere)
- **Automated Test Suite Status:** All passing.

---

## Deferred Issues Breakdown

| Rule Code | Category | Count | Primary Impact / Risk |
| :--- | :--- | :---: | :--- |
| **BLE001** | Blind Exception Catch (`except Exception:`) | 70 | Risk of unhandled exceptions terminating background workers or webhooks |
| **TRY002** | Raising Vanilla `Exception` in Tests | 8 | Risk of altering mock error strings relied upon by error classifiers |
| **DTZ006** | Naive `datetime.fromtimestamp()` | 1 | Potential timezone display discrepancy in Slack user interface |
| **Total** | | **79** | |

---

## 1. BLE001: Blind Exception Catch (`except Exception:`) — 70 Occurrences

### Description
Ruff flags catching `Exception` (`except Exception:` or `except Exception as e:`) as bad practice because it can catch unexpected system exceptions (such as `KeyboardInterrupt` or syntax/type errors in handlers) and hide bugs.

### Affected Locations
- **`services/slack_client.py`** (19 occurrences):
  - Lines 553, 630, 642, 781, 788, 824, 855, 906, 946, 949, 988, 1024, 1070, 1142, 1165, 1198, 1213, 1239, 1269
- **`orchestration/agent_settings.py`** (7 occurrences):
  - Lines 114, 303, 314, 341, 364, 398, 447
- **`services/pricing_service.py`** (6 occurrences):
  - Lines 94, 244, 263, 399, 433, 494
- **`services/web_tools.py`** (6 occurrences):
  - Lines 98, 130, 217, 236, 314, 457
- **`prompts/builder.py`** (5 occurrences):
  - Lines 35, 66, 97, 176, 194
- **`agents/usage_agent.py`** (4 occurrences):
  - Lines 31, 72, 89, 451
- **`main.py`** (4 occurrences):
  - Lines 208, 352, 363, 516
- **`services/mcp_tools.py`** (3 occurrences):
  - Lines 73, 102, 131
- **`agents/thread_notes_agent.py`** (2 occurrences):
  - Lines 209, 314
- **`core/model_catalog.py`** (2 occurrences):
  - Lines 44, 97
- **`nlp/language_detector.py`** (2 occurrences):
  - Lines 177, 190
- **`pipeline/entity_resolver/resolver.py`** (2 occurrences):
  - Lines 29, 37
- **`core/config.py`** (1 occurrence):
  - Line 82
- **`nlp/intent_extractor.py`** (1 occurrence):
  - Line 177
- **`orchestration/stages/execution.py`** (1 occurrence):
  - Line 158
- **`orchestration/stages/tool_status.py`** (1 occurrence):
  - Line 29
- **`orchestration/thread_state.py`** (1 occurrence):
  - Line 242
- **`orchestration/turn_pipeline.py`** (1 occurrence):
  - Line 292
- **`pipeline/discussion_scribe/scribe.py`** (1 occurrence):
  - Line 151
- **`skills/loader.py`** (1 occurrence):
  - Line 100

### Evaluation & Risk Analysis
1. **Critical Fault-Tolerance Boundaries:**
   In asynchronous architectures like Slack bots and agent orchestrators, background workers (`_usage_worker`, `_notes_worker`, turn loops) process queues indefinitely. If an unhandled exception escapes one of these loops, the entire worker task dies permanently for that thread or process, silently halting live notes or token tracking.
2. **Third-Party API Volatility:**
   Calls to external services (`slack_sdk.WebClient`, `litellm.completion`, `httpx`, `ddgs`, DuckDuckGo, MCP JSON-RPC) can raise dozens of unpredictable exceptions (e.g., `SlackApiError`, `socket.gaierror`, `httpx.RemoteProtocolError`, `json.JSONDecodeError`, `pydantic.ValidationError`, `anyio.EndOfStream`). Replacing `except Exception:` with a narrow set of exceptions risks leaving unforeseen network, SSL, or decoding errors unhandled, causing webhook timeouts or application crashes.
3. **Graceful Degradation:**
   In components like `prompts/builder.py` and `pipeline/entity_resolver/resolver.py`, `except Exception:` ensures that if reading custom configurations or dynamic agent schemas fails, the system falls back safely to default agent personas rather than crashing the user's turn.

### Recommended Next Steps
- **Do NOT remove or blindly narrow these `except Exception:` blocks.**
- If you wish to comply with BLE001 later, implement a unified top-level error wrapper pattern:
  ```python
  except (SpecificErrorA, SpecificErrorB) as e:
      # handle expected domain errors
  except Exception as e:
      logger.exception("[UNEXPECTED ERROR] Unhandled exception in worker boundary")
      # return safe fallback response
  ```

---

## 2. TRY002: Create Your Own Exception in Tests — 8 Occurrences

### Description
Ruff flags `raise Exception("...")` recommending that domain-specific or custom exception subclasses should be raised instead of vanilla `Exception`.

### Affected Locations
1. **`tests/test_router_circuit_breaker.py:81`**: `raise Exception("LLM should not be called when circuit-breaker is triggered")`
2. **`tests/test_router_error_handling.py:120`**: `raise Exception("AuthenticationError: Missing API key for Gemini")`
3. **`tests/test_router_error_handling.py:165`**: `raise Exception("NotFoundError: Model gemini/gemini-3.8-flash does not exist")`
4. **`tests/test_router_error_handling.py:212`**: `raise Exception("Internal Server Error: Unexpected JSON decoding crash")`
5. **`tests/test_router_error_handling.py:271`**: `raise Exception("RateLimitError: 429 Too Many Requests, quota exceeded")`
6. **`tests/test_router_error_handling.py:372`**: `raise Exception("ServiceUnavailableError: 503 The server is currently overloaded with high demand")`
7. **`tests/test_router_error_handling.py:442`**: `raise Exception("503 Server Temporarily Unavailable (high demand spike)")`
8. **`tests/test_router_error_handling.py:481`**: `raise Exception("503 Service Unavailable: overloaded")`

### Evaluation & Risk Analysis
1. **Test-Specific Mock Triggers:**
   All 8 occurrences are located strictly inside unit test files, where mock functions simulate upstream API failures from external providers (LiteLLM, OpenAI, Google Gemini, Anthropic).
2. **String Pattern Matching in Router:**
   The error classification subsystem (`core.llm_utils.classify_agent_error`) matches keywords like `"RateLimitError"`, `"AuthenticationError"`, and `"503"` directly against `str(e)`.
3. **Test Assertion Dependencies:**
   Tests such as `test_router_error_handling.py:484` assert:
   `with pytest.raises(Exception, match="503 Service Unavailable"):`
   Replacing generic `Exception` with a custom class could break existing test harness contracts and adds unnecessary boilerplate to simple test mocks.

### Recommended Next Steps
- Keep these as `Exception` in tests since they represent arbitrary external exceptions, or create a mock-only `MockAPIError(Exception)` helper if you decide to formalize test exceptions.

---

## 3. DTZ006: Naive `datetime.fromtimestamp()` — 1 Occurrence

### Description
Ruff flags `datetime.fromtimestamp(ts_float)` without a `tz` argument because naive datetimes depend on the host operating system's local timezone.

### Affected Location
- **`agents/usage_agent.py:27`**:
  ```python
  def parse_thread_creation_time(thread_ts: str) -> str:
      ts_float = float(thread_ts)
      dt = datetime.fromtimestamp(ts_float)
      return dt.strftime("%Y-%m-%d %H:%M:%S")
  ```

### Evaluation & Risk Analysis
- **Behavioral Impact on Slack UI:**
  `parse_thread_creation_time` formats thread start timestamps for display in Slack thread usage summaries.
  If changed to `datetime.fromtimestamp(ts_float, tz=timezone.utc)`, the displayed time will immediately shift by several hours (e.g. UTC+1/UTC+2 in Central Europe) compared to the server's local time.
- **Ambiguity:**
  The project does not currently configure a default timezone in `core/config.py`. Switching from naive local time to UTC without explicit user confirmation could cause confusion for end users reading thread summaries in Slack.

### Recommended Next Steps
- Clarify whether all timestamps displayed in Slack should be in UTC (`tz=timezone.utc`) or formatted with an explicit timezone offset (`"%Y-%m-%d %H:%M:%S %Z"`).

---

## Resolved Previously Deferred Items: S110 (`try`-`except`-`pass`)

The 11 original `S110` occurrences were resolved cleanly per instructions without disabling linter rules:
1. **Dynamic Log Level**: Configured `LOG_LEVEL` environment variable defaulting to `INFO` (added to `.env` and `.env.example`, loaded in `core/config.py` and `main.py`).
2. **`logger.debug(...)` Integration**: Replaced `pass` inside all 11 `try-except` blocks with informative `logger.debug(...)` statements:
   - `core/config.py:83`
   - `main.py:362`
   - `orchestration/agent_settings.py:112`
   - `pipeline/entity_resolver/resolver.py:27` and `34`
   - `prompts/builder.py:31`, `62`, and `95`
   - `services/slack_client.py:628`, `638`, and `944`
3. **Result**:
   - In normal operation (`LOG_LEVEL=INFO`), these debug statements are completely hidden, preventing log pollution.
   - When troubleshooting with `LOG_LEVEL=DEBUG`, fallback events become fully traceable.
   - `ruff check --select S110 .` passes with 0 errors.

---
*Report generated automatically following repository-wide lint analysis.*
