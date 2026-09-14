---
name: grant_for_all
display_name: Grant For All
description: Grant one or more skills to all agents in this specific thread for the configured expiration period.
scope: orchestrator
triggers:
  slash:
    - /grant_for_all
    - /grant-for-all
  arg_type: list
  param_name: skills
  auto_detect: false
---

### GRANT FOR ALL SKILL
Grants specified skills to all agents in the current Slack discussion thread.
Usage: `/grant_for_all [skill1] [skill2] ...`
