---
name: revoke_skill_for_all
display_name: Revoke Skill For All
description: Revoke one or more thread-global skills (or all skills) from all agents in this thread.
scope: orchestrator
triggers:
  slash:
    - /revoke_skill_for_all
    - /revoke-skill-for-all
  arg_type: list
  param_name: skills
  auto_detect: false
---

### REVOKE SKILL FOR ALL
Revokes specified skills or all skills from all agents in the current Slack discussion thread.
Usage: `/revoke_skill_for_all [skill1] [skill2] ...` or `/revoke_skill_for_all all`
