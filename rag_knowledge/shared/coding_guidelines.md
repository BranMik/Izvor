# Coding Guidelines & Standards

## 1. Python Best Practices
- Use Python 3.11+ type annotations consistently across function signatures.
- Prefer asynchronous I/O (`asyncio`, `httpx`, `async with`) for all network operations.
- Catch specific exceptions rather than bare `except:`; preserve error traceability with logging.

## 2. Code Quality & Architecture
- Maintain single-responsibility modular services under `services/`.
- Keep configuration centralized in `core/config.py` and `.env`.
- Ensure all public functions and classes include clear docstrings.

