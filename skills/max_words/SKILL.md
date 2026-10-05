---
name: max_words
display_name: Maximum Word Count Limit
description: Restrict the maximum word ceiling of the agent's response.
scope: prompt_modifier
triggers:
  auto_detect: true
  slash:
    - /max_words
    - /max-words
    - /maxwords
  arg_type: int_or_off
  param_name: max_words
extraction_rule: "Extract integer ceiling only if user requested a maximum length (e.g. 'in under 50 words', 'max 30 words', 'briefly in 40 words'). Do NOT extract minimum length as max_words."
requires_human_ITL: false
---

### OUTPUT CONSTRAINT: MAX WORDS
Hard Limit: Your entire response must strictly NOT exceed {value} words.
