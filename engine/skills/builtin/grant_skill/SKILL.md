---
name: grant_skill
display_name: Grant Skill
description: Grant a skill to specific agent(s) in this thread for the configured expiration period.
scope: orchestrator
triggers:
  slash:
    - /grant_skill
    - /grant-skill
  arg_type: grant_skill
  auto_detect: false
requires_human_ITL: false
requires_admin: true
---

### GRANT SKILL
Grants a specified skill to one or more agents in the current Slack discussion thread for the thread expiration period.
Usage: `/grant_skill [skill] [agent] [agent]...`
