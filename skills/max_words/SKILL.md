---
name: max_words
display_name: Maximum Word Count Limit
description: Restrict the maximum word ceiling of the agent's response.
scope: prompt_modifier
triggers:
  slash:
    - /max_words
    - /max-words
    - /maxwords
  arg_type: int
---

### OUTPUT CONSTRAINT: MAX WORDS
Hard Limit: Your entire response must strictly NOT exceed {value} words.
