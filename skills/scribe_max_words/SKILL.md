---
name: scribe_max_words
display_name: Discussion Scribe Maximum Words
description: Restrict the maximum word ceiling of the Scribe's strategic synthesis notes.
scope: orchestrator
triggers:
  slash:
    - /scribe_max_words
    - /scribe-max-words
    - /scribemaxwords
  arg_type: int
  param_name: words
---

### DISCUSSION SCRIBE MAXIMUM WORDS
Restricts the maximum word ceiling for the Live Discussion Scribe synthesis notes in the thread.
Usage: `/scribe_max_words 50` or `/scribe_max_words off`
