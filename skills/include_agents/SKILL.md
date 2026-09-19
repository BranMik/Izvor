---
name: include_agents
display_name: Include Agents in Debate
description: Include or add specific agents into the debate regardless of workflow, overriding previous exclusions.
scope: orchestrator
triggers:
  slash:
    - /include
  arg_type: list
  param_name: agents
---

### INCLUDE AGENTS
Forces the specified agents into the debate participant roster regardless of workflow, and overrides any previous exclusions for those agents.
Usage: `/include [Agent1] [Agent2]` or `/include off`
