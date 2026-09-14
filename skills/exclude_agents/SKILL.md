---
name: exclude_agents
display_name: Exclude Agents from Debate
description: Exclude or leave out specific agents from participating in the thread discussion.
scope: orchestrator
triggers:
  slash:
    - /exclude
  arg_type: list
  param_name: agents
---

### EXCLUDE AGENTS
Omits the specified agents from the active debate participant roster.
