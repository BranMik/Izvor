---
name: knowledge_base_rag
display_name: Knowledge Base RAG
description: Progressive disclosure skill to search organizational documents, architecture specifications, coding standards, and agent domain runbooks using hybrid dense vector and BM25 full-text search.
scope: agent
tools:
  - search_knowledge_rag
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
  - `"all"`: Searches both your role-specific documentation (`rag_knowledge/{YourAgentName}/`) and shared organizational knowledge (`rag_knowledge/shared/`).
  - `"self"`: Restricts the search strictly to your role-specific documentation.
- **`max_results`** (integer, optional, default: `4`): Number of most relevant excerpts to retrieve.

## Best Practices
1. Formulate concise, targeted queries matching the technical terms you are seeking.
2. Always attribute retrieved information citing the document filename and section header returned by the tool.
3. If search yields no results with `"self"`, consider broadening your scope to `"all"`.
