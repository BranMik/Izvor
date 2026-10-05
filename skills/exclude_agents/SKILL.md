---
name: exclude_agents
display_name: Exclude Agents from Debate
description: Exclude or leave out specific agents from participating in the thread discussion.
scope: orchestrator
triggers:
  auto_detect: true
  slash:
    - /exclude
  arg_type: agent_list
  param_name: agents
extraction_rule: "If user asks to exclude or leave out specific agents, include action '{name}' with param 'agents' (list of string agent keys). Agents tagged to speak must NEVER be excluded."
requires_human_ITL: false
---

### EXCLUDE AGENTS
Omits the specified agents from the active debate participant roster.
