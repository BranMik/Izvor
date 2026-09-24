# QA Lead Test Strategy & Verification Plan

## Test Levels
- **Unit Tests**: Test each module in isolation using `pytest`. Aim for high coverage on pure algorithmic and parsing functions.
- **Integration Tests**: Verify vector database indexing, manifest diffing, and search ranking with mock embeddings.
- **End-to-End Tests**: Simulate agent prompt queries against mock or live Slack threads.

## Acceptance Criteria
- 100% of manifest reconciliation tests pass.
- No memory leaks or unclosed file descriptors during repeated startup passes.
