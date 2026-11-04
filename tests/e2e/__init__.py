"""
Izvor - End-to-End Hybrid Simulation Test Harness.

A standalone, regression-ready testing framework simulating the Slack protocol with 100% fidelity:
- Authentic Slack Events API and Block Kit Interactivity payloads with HMAC-SHA256 signatures.
- Hybrid LLM execution (live LLM for intent/language/tool-inference, mock fixtures for verbose discussion).
- Deterministic MCP tool fixtures.
- Outbound Slack message & interactive button verification.
"""

__version__ = "1.0.0"
