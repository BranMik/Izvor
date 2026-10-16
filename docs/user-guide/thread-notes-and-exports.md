# Thread Notes, Exports & Scribe Summaries

This guide explains how **Izvor** captures debate history, generates live and closing executive summaries via the Scribe persona, and asynchronously archives flat markdown transcripts.

---

## ✍️ The Scribe Persona & Debate Closure

When a deliberation is initiated, progresses, or reaches its configured round limit, Izvor engages the **Scribe** persona:

### 1. Live Discussion Notes & Real-Time Tracking
- During active debates, the Scribe can maintain a rolling live summary message tracking key discussion points as arguments evolve.
- Word constraints (`scribe_min_words`, `scribe_max_words`) can be specified to tune synthesis depth.

### 2. Conclusion Note & Decision Pinning
- When a debate concludes (either by reaching round limits or upon `/stop`), the Scribe produces an authoritative closing synthesis:
  - **Key Decision Points**: Options and architectural alternatives weighed.
  - **Consensus & Tradeoffs**: Points of consensus, dissenting opinions, and accepted risks.
  - **Action Items & Owners**: Assigned follow-ups and immediate next steps.
- The conclusion note is formatted with high-contrast Slack Block Kit headers and can be pinned to the thread for executive review.

---

## 📁 Asynchronous Flat History Markdown Archival

Every deliberation turn is automatically and asynchronously archived to a local markdown transcript by [`services/flat_history_recorder.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/flat_history_recorder.py).

### 1. Non-Blocking Off-Loop Execution
Disk I/O is decoupled from the main asyncio event loop using `async_record_flat_history`:
```python
await run_in_threadpool(record_flat_history, channel_id, thread_ts, turn_data)
```
This ensures disk writes never introduce jitter or latency into the Slack event dispatcher.

### 2. Storage Location & Naming
Transcripts are persisted in the `flat-histories/` directory using slugified names:
```
flat-histories/<channel_prefix>_<thread_timestamp>.md
```
*Example*: `flat-histories/eng_architecture_1790455185.md`

### 3. File Format Structure
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

## 📥 File Attachments & Untrusted Content Wrapping

When users share files in Slack threads:
1. Izvor's file listener downloads the attachment to `downloads/`.
2. The [`document_reader`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/document_reader) parses the document content (PDFs, Markdown, CSVs, code files).
3. Extracted text is wrapped in `<untrusted_file_content>` security boundaries, enabling debate personas to critique the uploaded RFC or specification without risking prompt injection.

---

## 🔄 Archival & Knowledge Base Sync

Exported flat histories can be ingested into Izvor’s own RAG knowledge base:
- Move or copy completed transcripts into `rag_knowledge/shared/`.
- Trigger reconciliation via `POST /api/rag/reconcile` (or wait for the automatic startup sync).
- Future deliberations can immediately cite outcomes and rationale from past debates.
