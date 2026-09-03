---
name: spend-governance
description: Real-time multi-model token ledger, monetary expense calculator, and budget circuit-breaker guardrail.
---

# Spend Governance Skill

## Purpose
Multi-agent discussions can trigger recursive agent chains, deep tool calls, or runaway token loops. This skill acts as an enterprise guardrail to track costs and enforce strict spending ceilings.

## Capabilities
1. **Per-Model Token Accounting**: Tracks prompt and completion tokens across diverse model providers (Google Gemini, OpenAI, Anthropic, Mistral, Groq, Meta).
2. **Dynamic Pricing Resolution**: Leverages cached and web-discovered model rate cards to calculate exact monetary spend in USD.
3. **Hard Circuit Breaker**: Pre-turn assertions that halt execution when a session cost reaches or exceeds the configured threshold ceiling (e.g. `$0.50`, `$1.00`), preventing surprise cloud bills.
