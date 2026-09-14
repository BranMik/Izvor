---
name: revoke_for_all
display_name: Revoke For All
description: Revoke one or more thread-global skills (or all skills) from all agents in this thread.
scope: orchestrator
triggers:
  slash:
    - /revoke_for_all
    - /revoke-for-all
  arg_type: list
  param_name: skills
  auto_detect: false
---

### REVOKE FOR ALL SKILL
Revokes specified skills or all skills from all agents in the current Slack discussion thread.
Usage: `/revoke_for_all [skill1] [skill2] ...` or `/revoke_for_all all`
