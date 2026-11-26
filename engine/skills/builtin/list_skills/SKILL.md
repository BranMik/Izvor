---
name: list_skills
display_name: List Skills
description: List all available skills in the application along with the agents authorized to use each skill.
scope: orchestrator
triggers:
  slash:
    - /list_skills
    - /list-skills
    - /skills
  arg_type: none
  auto_detect: false
requires_human_ITL: false
requires_admin: false
---

### LIST SKILLS
Provides a complete, transparent directory of all skills and directives available across the platform, including descriptions and which agents are authorized to use each skill.

Usage:
- Slash command: `/list_skills` (or `/list-skills`, `/skills`) deterministically posts the thread-aware skills directory directly from `SkillRegistry` (no LLM call) and halts further agent turns.
