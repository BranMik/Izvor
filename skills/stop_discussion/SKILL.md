---
name: stop_discussion
display_name: Stop Discussion
description: Immediately stop and terminate the active thread debate and chained panel responses.
scope: orchestrator
triggers:
  slash:
    - /stop
  terminal: true
---

### STOP DISCUSSION
Terminates panel turn execution and stops ongoing multi-agent discussion loop.
