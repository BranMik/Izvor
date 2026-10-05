---
name: revoke_constraint
display_name: Revoke Thread Constraints
description: Revoke active thread constraints, word limits, exclusions, inclusions, or all constraints.
scope: orchestrator
triggers:
  slash:
    - /revoke_constraint
    - /revoke-constraint
  arg_type: token
  param_name: target
  auto_detect: false
requires_human_ITL: false
---

### REVOKE CONSTRAINT
Revokes a specific active thread constraint or all constraints.
Usage: `/revoke_constraint [max_words | min_words | scribe_max_words | scribe_min_words | exclude | include | language | all]`
