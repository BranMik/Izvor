# Thread Notes, Exports & Scribe Summaries

This guide explains how **Izvor** captures debate history, generates live and closing executive summaries via the Scribe persona, and asynchronously archives flat markdown transcripts.

---

## ✍️ The Scribe Persona & Debate Closure

> [!NOTE]
> The Discussion Scribe uses the dedicated `DiscussionScribe` system-agent configuration in [`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json) (default: `mistral/mistral-small-latest`), independent of heavy reasoning models, with automatic fallback and empty-output protection.

When a deliberation is initiated, progresses, or reaches its configured round limit, Izvor engages the **Scribe** persona:

### 1. Live Discussion Notes & Real-Time Tracking
- During active debates, the Scribe maintains a rolling live summary message tracking key discussion points as arguments evolve (strictly under 300 words final, 220 words ongoing, 50 words on initial turn):
  - **Core Objective & Problem Statement** (max 35 words): 1–2 concise sentences.
  - **Key Perspectives** (max 190 words total; max 30 words per bullet): 1 compact bullet per speaking agent.
  - **Risks & Open Questions** (max 75 words): Unresolved risks or questions raised during deliberation.
- Empty sections are automatically omitted if no substantive points exist.
- Word constraints (`scribe_min_words`, `scribe_max_words`) can be specified to tune synthesis depth.

### 2. Conclusion Note & Decision Pinning
- When a debate concludes (either by reaching round limits or upon `/stop`), the Scribe produces an authoritative closing synthesis (strictly under 320 words):
  - **Problem Statement** (max 25 words): Single-sentence executive framing.
  - **Consensus Decisions** (max 85 words): Unanimous or broad-agreement positions.
  - **Divergent Positions & Trade-offs** (max 140 words): Compact bullet per dissenting or alternative recommendation with agent attribution, evidence, and counter-arguments.
  - **Action Items & Next Steps** (max 70 words): Concrete, prioritized follow-up steps.
- The conclusion note is formatted with high-contrast Slack Block Kit headers, supports multi-block splitting up to 3,800 characters to prevent truncation, and is pinned to the thread for executive review.

### 3. Thread Notes Disk Persistence ([`services/thread_notes_writer.py`](file:///engine/services/thread_notes_writer.py))
In addition to posting inside Slack, thread notes are automatically saved to disk in `agents_resources/thread_notes/`:
- **Live Notes**: `<prompt_slug>_<ts5>_live_notes.md`
- **Conclusion Notes**: `<prompt_slug>_<ts5>_conclusion.md`
*(where `<ts5>` represents the last 5 digits of the thread timestamp, preventing accidental filename collisions even if identical prompts are initiated)*.

---

## 📁 Asynchronous Flat History Markdown Archival

Every deliberation turn is automatically and asynchronously archived to a local markdown transcript by [`services/flat_history_recorder.py`](file:///engine/services/flat_history_recorder.py).

### 1. Non-Blocking Execution
Disk I/O is dispatched asynchronously via `async_record_flat_history`:
```python
await async_record_flat_history(channel_id, thread_ts, initial_prompt, messages)
```
This ensures disk writes never introduce jitter or latency into the Slack event dispatcher.

### 2. Storage Location & Naming
Transcripts are persisted in the `data/flat-histories/` directory using prompt slugs and thread timestamps:
```
data/flat-histories/<prompt_prefix_10>_<thread_ts_10>.md
```
*Example*: `data/flat-histories/we_are_cons_1790455185.md` (or `data/flat-histories/we_are_cons_1790455185_54321.md` if disambiguating collisions).

### 3. File Format Structure
```markdown
# Conversation History: 1790455185.000100

- **Thread TS:** `1790455185.000100`
- **Channel ID:** `C0123456789`
- **Last Updated:** `2026-10-08 10:14:15 UTC`
- **Initial Prompt:** We are considering migrating our session store from Redis to Postgres row-level JSONB.

---

### **[User]**

We are considering migrating our session store from Redis to Postgres row-level JSONB. What are the engineering tradeoffs, security considerations, and cost impacts?

---

### **[Visionary]**

Adopting Postgres JSONB unifies our storage topology and simplifies transactional consistency...

---

### **[Security Architect]**

While attractive, session tokens stored in row JSONB require strict field-level encryption...

---
```

---

## 📥 File Attachments & Untrusted Content Wrapping

When users share files in Slack threads:
1. Izvor's file listener downloads the attachment to `data/downloads/`.
2. The [`document_reader`](file:///config/skills/document_reader) parses the document content (PDFs, Markdown, CSVs, code files).
3. Extracted text is wrapped in `<untrusted_file_content>` security boundaries, enabling debate personas to critique the uploaded RFC or specification without risking prompt injection.

---

## 🔄 Archival & Knowledge Base Sync

Exported flat histories can be ingested into Izvor’s own RAG knowledge base:
- Move or copy completed transcripts into `config/rag_knowledge/shared/`.
- Trigger reconciliation via `POST /api/rag/reconcile` (or wait for the automatic startup sync).
- Future deliberations can immediately cite outcomes and rationale from past debates.
