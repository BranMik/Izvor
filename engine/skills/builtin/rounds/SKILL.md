---
name: rounds
display_name: Discussion Rounds
description: Set or continue multi-agent debate rounds from the first to the last agent.
scope: orchestrator
triggers:
  slash:
    - /rounds
    - /round
    - /next_round
    - /next-round
    - /nextround
  auto_detect: false
  arg_type: int
  param_name: rounds
requires_human_ITL: false
---

### DISCUSSION ROUNDS
Controls multi-agent debate rounds. Loops or continues the discussion from the first agent to the last agent for the specified number of rounds.
