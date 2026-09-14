---
name: min_words
display_name: Minimum Word Count Floor
description: Require a minimum word count floor for the agent's response.
scope: prompt_modifier
triggers:
  slash:
    - /min_words
    - /min-words
    - /minwords
  arg_type: int
---

### OUTPUT CONSTRAINT: MIN WORDS
Minimum Length: Your entire response must have a minimum length of {value} words.
