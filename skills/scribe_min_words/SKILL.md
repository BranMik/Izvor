---
name: scribe_min_words
display_name: Discussion Scribe Minimum Words
description: Require a minimum word count floor for the Scribe's strategic synthesis notes.
scope: orchestrator
triggers:
  slash:
    - /scribe_min_words
    - /scribe-min-words
    - /scribeminwords
  arg_type: int
  param_name: words
---

### DISCUSSION SCRIBE MINIMUM WORDS
Requires a minimum word count floor for the Live Discussion Scribe synthesis notes in the thread.
Usage: `/scribe_min_words 50` or `/scribe_min_words off`
