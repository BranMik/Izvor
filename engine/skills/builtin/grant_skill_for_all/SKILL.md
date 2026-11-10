---
name: grant_skill_for_all
display_name: Grant Skill For All
description: Grant one or more skills to all agents in this specific thread for the configured expiration period.
scope: orchestrator
triggers:
  slash:
    - /grant_skill_for_all
    - /grant-skill-for-all
    - /grant_for_all
  arg_type: list
  param_name: skills
  auto_detect: false
requires_human_ITL: false
requires_admin: true
---

### GRANT SKILL FOR ALL
Grants specified skills to all agents in the current Slack discussion thread.
Usage: `/grant_skill_for_all [skill1] [skill2] ...`
