---
name: min_words
display_name: Minimum Word Count Floor
description: Require a minimum word count floor for the agent's response.
scope: prompt_modifier
triggers:
  auto_detect: true
  slash:
    - /min_words
    - /min-words
    - /minwords
    - /min
  arg_type: int_or_off
  param_name: min_words
extraction_rule: "Extract integer floor only if user requested a minimum length (e.g. 'at least 100 words', 'minimalno 50 riječi')."
requires_human_ITL: false
---

### OUTPUT CONSTRAINT: MIN WORDS
Minimum Length: Your entire response must have a minimum length of {value} words.
