---
name: stop_discussion
display_name: Stop Discussion
description: Immediately stop and terminate the active thread debate and chained panel responses.
scope: orchestrator
triggers:
  slash:
    - /stop
  auto_detect: false
  terminal: true
  arg_type: terminal_flag
---

### STOP DISCUSSION
Terminates panel turn execution and stops ongoing multi-agent discussion loop.
