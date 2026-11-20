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
extraction_rule: "If the user explicitly asks in natural language to forcefully include, bring in, or add extra agents to the discussion, include action '{name}' with param 'agents'. NEVER emit '{name}' merely because an agent is addressed via a '/[AgentName]' speaker tag."
requires_human_ITL: false
requires_admin: false
---

### INCLUDE AGENTS
Forces the specified agents into the debate participant roster regardless of workflow, and overrides any previous exclusions for those agents.
Usage: `/include [Agent1] [Agent2]` or `/include off`
