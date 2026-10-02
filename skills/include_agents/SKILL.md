---
name: include_agents
display_name: Include Agents in Debate
description: Include or add specific agents into the debate regardless of workflow, overriding previous exclusions.
scope: orchestrator
triggers:
  auto_detect: true
  slash:
    - /include
  arg_type: agent_list
  param_name: agents
extraction_rule: "If user asks to forcefully include, bring in, or add specific agents to the discussion, include action '{name}' with param 'agents'."
---

### INCLUDE AGENTS
Forces the specified agents into the debate participant roster regardless of workflow, and overrides any previous exclusions for those agents.
Usage: `/include [Agent1] [Agent2]` or `/include off`
