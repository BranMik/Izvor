---
name: forget_thread
display_name: Forget Thread (GDPR Purge)
description: Permanently purge all persisted state, flat histories, thread notes, and thread downloads for the current thread (channel-scoped GDPR erasure).
scope: orchestrator
triggers:
  slash:
    - /forget_thread
    - /forget-thread
  arg_type: none
  auto_detect: false
requires_human_ITL: false
requires_admin: true
---

### FORGET THREAD
Permanently deletes all persisted history, thread notes, downloaded attachments, database records, and in-memory state associated with the current Slack channel thread (`channel_id` + `thread_ts`).

Usage:
- Explicit admin slash command only: `/forget_thread` (or `/forget-thread`).
- Never triggered automatically via natural language inference.

