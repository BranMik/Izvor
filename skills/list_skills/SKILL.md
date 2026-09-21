---
name: list_skills
display_name: List Skills
description: List all available skills in the application along with the agents authorized to use each skill.
scope: agent
triggers:
  slash:
    - /list_skills
    - /list-skills
  arg_type: none
  auto_detect: false
---

### LIST SKILLS
Provides a complete, transparent directory of all skills and directives available across the platform, including descriptions and which agents are authorized to use each skill.

Usage:
- Slash command: `/list_skills` (lists all skills via the system model and halts further agent turns if invoked without an agent tag or additional prompt text).
- Targeted query: `[Agent] /list_skills` or ask any agent (e.g., "What skills are available in the app?") to have the agent present the skills directory.
