# ADR 001: Modular Agent-Oriented RAG Architecture

## Context & Problem Statement
Agents in the panel need access to organizational domain knowledge and role-specific runbooks without overflowing context windows or causing high latency during startup.

## Decision
1. Implement embedded LanceDB with hybrid search (dense embeddings + BM25 full-text rank).
2. Utilize progressive disclosure (`knowledge_base_rag` skill) so agents query knowledge only when necessary.
3. Organize documents by agent folder (`rag_knowledge/{AgentName}/`) and shared folder (`rag_knowledge/shared/`).
4. Reconcile documents asynchronously at startup using SHA-256 content hashes.

## Consequences
- Fast sub-second response times.
- Zero server daemons required.
- Clear authorization boundaries per agent.
