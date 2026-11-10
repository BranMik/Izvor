---
name: first
display_name: First Speaker
description: Designate the initial speaker for the debate, continuing in normal workflow order.
scope: orchestrator
triggers:
  auto_detect: false
  slash:
    - /first
  arg_type: agent_token
  param_name: first_agent
requires_human_ITL: false
requires_admin: false
---

### FIRST SPEAKER
Sets the specified agent as the first speaker to start the panel discussion, after which the debate continues in its normal workflow order.
