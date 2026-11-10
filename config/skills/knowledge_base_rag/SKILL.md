---
name: knowledge_base_rag
display_name: Knowledge Base RAG
description: Progressive disclosure skill to search organizational documents, architecture specifications, coding standards, and agent domain runbooks using hybrid dense vector and BM25 full-text search.
scope: agent
triggers:
  auto_detect: true
  slash:
    - /rag
    - /search_docs
    - /knowledge
extraction_rule: "If user asks to search or query internal documentation, knowledge base, specs, runbooks, playbooks, or reference guides, include action 'knowledge_base_rag'."
tools:
  - search_knowledge_rag
requires_human_ITL: false
requires_admin: false
---

# Knowledge Base RAG Operational Guidelines

This skill provides on-demand access to verified project and organizational knowledge via the `search_knowledge_rag` tool.

## When to Use This Skill
- You need verified technical specifications, architecture decisions (ADRs), or project standards.
- You need your role's specific runbooks, checklists, or workflow guidelines.
- You need to cite concrete facts, policies, or procedures rather than hallucinating details.

## Tool: `search_knowledge_rag`
- **`query`** (string, required): A natural language question or specific technical keyword (e.g. `"rollback procedures"`, `"data isolation policy"`, `"sprint guidelines"`).
- **`scope`** (string, optional, default: `"all"`):
  - `"all"`: Searches both your role-specific documentation (`rag_knowledge/{agent_key}/`) and shared organizational knowledge (`rag_knowledge/shared/`).
  - `"self"`: Restricts the search strictly to your role-specific documentation.
- **`max_results`** (integer, optional, default: `6`): Number of most relevant excerpts to retrieve.

## Mandatory Operational Rules
1. **Targeted Queries**: Formulate concise queries matching the exact technical or strategic terms you are seeking. If search yields no results with `"self"`, broaden scope to `"all"`.
2. **Direct Relevance & Category Fidelity**: Focus your response directly on the specific section, step, or framework that explicitly answers the user's inquiry (e.g. when asked how AI makes money, present the dedicated financial levers defined in that step, rather than wandering into unrelated steps or tangential examples). Maintain high fidelity to the source: use the exact framework names, categories, numbers, and key phrasing from the document instead of inventing your own categories or paraphrasing them into generic concepts.
3. **Mandatory Citations**: You MUST attribute all retrieved facts, use cases, metrics, or guidelines by citing the exact document path and section header at the end of your response (e.g. `_Source: `agent-10008/security_policy.md` (Section General)_`). Never omit this source citation.
