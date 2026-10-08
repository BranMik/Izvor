# Spend Governance & Pricing Engine

This document details how **Izvor** monitors LLM token usage, calculates real-time inference expenses across multiple providers, and enforces budget circuit breakers.

---

## 💰 Architecture Overview

Izvor orchestrates multiple LLM personas across providers such as OpenRouter, OpenAI, Anthropic, Google Gemini, and Mistral. To protect against runaway costs or looped tool executions, the platform includes an autonomous spend governance subsystem:

```mermaid
graph TD
    Turn[Completed Agent Turn] --> Usage[Extract Token Counts: Prompt & Completion]
    Usage --> PricingService[PricingService (services/pricing_service.py)]
    
    subgraph Pricing Service Architecture
        PricingService --> CacheCheck{Cache < 7 Days?}
        CacheCheck -->|Yes| DiskCache[Read data/model_pricing_cache.json]
        CacheCheck -->|No / Miss| Researcher[Autonomous Pricing Researcher Agent]
        Researcher --> WebQuery[Query Provider Official Rate Tables]
        Researcher --> UpdateCache[Save Updated Rates to Disk Cache]
    end
    
    PricingService --> ComputeUSD[Compute Exact USD Cost for Turn]
    ComputeUSD --> Accumulator[Add to Thread Cumulative Spend]
    Accumulator --> CircuitBreaker{Exceeds Threshold?}
    CircuitBreaker -->|Yes| TripBreaker[Halt Debate & Post Warning in Thread]
    CircuitBreaker -->|No| Proceed[Proceed to Next Turn]
```

---

## 🤖 The Autonomous Pricing Researcher Agent

Unlike static cost tables that go out of date as providers adjust their prices, Izvor features an autonomous pricing researcher agent:

1. **Prompt Template**: Defined in [`prompts/system_agents/Pricing_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts/system_agents/Pricing_Agent.md).
2. **Execution**: Dispatched by [`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/pricing_service.py) during application startup or when an unindexed model is encountered.
3. **Web Research**: Utilizes web search tools to locate official provider pricing tables (per 1,000 or per 1,000,000 tokens for prompt and completion).
4. **Caching**: Results are stored in `data/model_pricing_cache.json` with a 7-day TTL.
5. **Hardcoded Fallbacks**: If web access is unavailable, baseline fallbacks for standard flagship models (Claude 3.5 Sonnet, GPT-4o, Gemini 1.5 Pro) are loaded safely.

---

## 📊 Token & Cost Tracking Per Turn

After each completed agent turn, [`orchestration/stages/post_record.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/post_record.py) records:
- **`prompt_tokens`**: Input tokens processed by the LLM.
- **`completion_tokens`**: Output tokens generated in the reply.
- **`model_name`**: The exact model identifier invoked.
- **`turn_cost_usd`**: Calculated using:
  $$\text{Cost} = \left(\text{Prompt Tokens} \times \text{Rate}_{\text{prompt}}\right) + \left(\text{Completion Tokens} \times \text{Rate}_{\text{completion}}\right)$$
- **Cumulative Metrics**: Stored in [`SpendMetrics`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/core/domain.py) on the thread's session context.

---

## ⚡ Circuit Breaker Protection

To safeguard budgets, Izvor includes an automated circuit breaker:

### 1. Configuration
Admins configure circuit breaker rules globally or via Slack App Home:
- **`circuit_breaker_enabled`**: Boolean toggle.
- **`max_thread_spend_usd`**: Maximum allowed USD cost per thread (e.g., `$1.50`).

### 2. Tripping the Breaker
Before starting any new turn, [`orchestration/stages/validation.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/validation.py) evaluates:
```python
if thread_spend.total_cost_usd >= circuit_breaker_threshold:
    raise CircuitBreakerTrippedException(...)
```

When tripped:
- Further LLM calls in the thread are immediately halted.
- A notification block is posted to the Slack thread informing participants:
  > *⚠️ Spend Circuit Breaker Triggered: This thread has reached its configured budget limit ($1.50). Further automated debate turns are paused.*
- Admins can raise the limit or resume deliberation directly via Slack App Home.
