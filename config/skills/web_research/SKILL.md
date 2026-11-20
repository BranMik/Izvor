---
name: web_research
display_name: Web Research
description: Search the live web and extract clean text content from web pages for real-time intelligence, competitor pricing, documentation, and external benchmarks.
scope: agent
triggers:
  slash:
    - /web_research
    - /webresearch
  auto_detect: true
  param_name: query
extraction_rule: "If user asks to search the live web, check current pricing, or find recent external news/benchmarks, include action '{name}' with param 'query'."
tools:
  - web_search
  - fetch_web_page
requires_human_ITL: false
requires_admin: false
---

# Web Research Skill

## Purpose & When to Use
Use this skill when you need real-time external data, current pricing benchmarks, competitor solutions, recent announcements, or provider documentation.

## Capabilities & Tool Signatures
- `web_search(query: str, max_results: int = 5)`: Returns top web search results with title, URL, and snippet.
- `fetch_web_page(url: str, max_chars: int = 4000)`: Extracts clean, readable text from a URL, stripping HTML tags, scripts, and navigation.

## Operational Guidelines
1. **Formulate concise, targeted queries**: Focus on concrete search terms (e.g. `"Anthropic Claude pricing 2025"`, `"FastAPI vs Litestar benchmarks"`).
2. **Respect the 5-request per-turn web budget (`max_fetches_per_turn = 5`)**: Issue at most **1–2 high-signal `web_search` queries per turn** rather than multiple redundant keyword variations. Reserve remaining turn budget for `fetch_web_page` and synthesize immediately once relevant snippets are gathered.
3. **Deep-dive on high-value results**: When a snippet indicates high relevance, invoke `fetch_web_page` on that URL to extract full facts.
4. **Always cite sources**: When presenting findings from web search, cite the source name and URL.
5. **Synthesize**: Condense raw web text into concise, actionable takeaways for the discussion.
