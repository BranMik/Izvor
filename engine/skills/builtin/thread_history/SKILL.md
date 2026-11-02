---
name: thread_history
display_name: Thread History Slicing
description: Limit the number of prior conversation messages considered in this thread.
scope: orchestrator
triggers:
  auto_detect: true
  slash:
    - /history
  arg_type: int
  param_name: history_limit
extraction_rule: "Extract integer message limit if requested (e.g. 'last 3 messages', 'zadnje 2 poruke')."
requires_human_ITL: false
---

### THREAD HISTORY LIMIT
Restricts active discussion context to the specified number of recent messages.
