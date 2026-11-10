---
name: open_debate
display_name: Open Debate
description: Designate the initial speaker and run an open debate without fixed start/end workflow chains, allowing agents to dynamically nominate peers.
scope: orchestrator
triggers:
  auto_detect: false
  slash:
    - /open_debate
    - /open-debate
    - /opendebate
  arg_type: agent_token
  param_name: first_agent
requires_human_ITL: false
requires_admin: false
---

### OPEN DEBATE
Designates the specified agent as the first speaker and bypasses fixed start/end workflow chains. All agents participate in an open dynamic debate where each speaker nominates the next.
