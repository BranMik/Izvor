# Thread Notes, Exports & Scribe Summaries

This guide explains how **Izvor** captures debate history, generates executive summaries via the Scribe persona, and archives flat markdown transcripts.

---

## ✍️ The Scribe Persona & Debate Closure

When a deliberation reaches its configured round limit or when a user requests synthesis, Izvor activates the **Scribe** persona:

1. **Comprehensive Context Review**: The Scribe reviews the entire thread chronology, distinguishing between unresolved tension and established consensus.
2. **Standardized Executive Structure**:
   - **Key Decision Points**: What options were weighed?
   - **Consensus & Tradeoffs**: Where did the team agree or accept risks?
   - **Action Items**: Assigned follow-ups, owners, and immediate next steps.
3. **Word Guardrails**: The Scribe respects `scribe_min_words` and `scribe_max_words` directives to deliver the appropriate depth of detail.

---

## 📁 Flat History Markdown Recording

Every deliberation turn is automatically archived to a local markdown file by [`services/flat_history_recorder.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/flat_history_recorder.py).

### 1. Storage Location & Naming
Transcripts are persisted in the `flat-histories/` directory using slugified names:
```
flat-histories/<channel_prefix>_<thread_timestamp>.md
```
*Example*: `flat-histories/eng_architecture_1790455185.md`

### 2. File Format Structure
```markdown
# Debate Transcript: Slack Thread #eng-architecture (ts: 1790455185)

## [2026-10-08 10:14:02 UTC] Human User (@alice)
We are considering migrating our session store from Redis to Postgres row-level JSONB...

---

## [2026-10-08 10:14:06 UTC] Visionary (Model: gemini/gemini-3.8-flash)
Adopting Postgres JSONB unifies our storage topology and simplifies transactional consistency...

---

## [2026-10-08 10:14:12 UTC] Pragmatist (Model: mistral/mistral-large-latest)
While attractive, high-frequency session writes cause table bloat and vacuum strain in Postgres...
```

---

## 📥 File Attachments & Downloads

When users share files in Slack threads:
1. Izvor's file listener downloads the attachment to `downloads/`.
2. The [`document_reader`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/document_reader) parses the document content.
3. Extracted text is fed into the context preparation stage, enabling all enabled debate personas to critique the uploaded RFC, PDF, or code snippet.

---

## 🔄 Archival & Knowledge Base Sync

Exported flat histories can be directly ingested into Izvor’s own RAG knowledge base:
- Move or copy completed transcripts into `rag_knowledge/shared/`.
- Trigger reconciliation via `POST /api/rag/reconcile` (or wait for the periodic sync).
- Future deliberations can immediately cite the outcomes of past debates!
