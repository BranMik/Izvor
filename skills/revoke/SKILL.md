---
name: revoke
display_name: Revoke Thread Constraints
description: Revoke active thread constraints, word limits, exclusions, inclusions, or all constraints.
scope: orchestrator
triggers:
  slash:
    - /revoke
  arg_type: str
  param_name: target
---

### REVOKE CONSTRAINT
Revokes a specific active thread constraint or all constraints.
Usage: `/revoke [max_words | min_words | scribe_max_words | scribe_min_words | exclude | include | language | all]`
