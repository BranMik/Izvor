---
name: scribe_max_words
display_name: Discussion Scribe Maximum Words
description: Restrict the maximum word ceiling of the Scribe's strategic synthesis notes.
scope: orchestrator
triggers:
  auto_detect: true
  slash:
    - /scribe_max_words
    - /scribe-max-words
    - /scribemaxwords
  arg_type: int_or_off
  param_name: words
extraction_rule: "Extract integer ceiling if user requested a maximum length specifically for the scribe/summary (e.g. '/scribe_max_words 50', 'scribe summary under 40 words')."
requires_human_ITL: false
requires_admin: false
---

### DISCUSSION SCRIBE MAXIMUM WORDS
Restricts the maximum word ceiling for the Live Discussion Scribe synthesis notes in the thread.
Usage: `/scribe_max_words 50` or `/scribe_max_words off`
