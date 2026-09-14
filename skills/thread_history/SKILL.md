---
name: thread_history
display_name: Thread History Slicing
description: Limit the number of prior conversation messages considered in this thread.
scope: orchestrator
triggers:
  slash:
    - /history
  arg_type: int
---

### THREAD HISTORY LIMIT
Restricts active discussion context to the specified number of recent messages.
