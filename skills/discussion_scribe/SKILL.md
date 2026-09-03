---
name: discussion-scribe
description: Autonomous observer skill for bilingual real-time meeting synthesis, progress notes, and final strategic takeaways.
---

# Discussion Scribe Skill

## Purpose
As multi-agent debates progress across technical perspectives, humans and observers require a living summary of consensus, divergence, and action items rather than reading hundreds of raw messages.

## Capabilities
1. **Language-Matching Synthesis**: Automatically detects whether the primary discussion is in Croatian or English and renders all headings and synthesized takeaways in that language.
2. **Delta-Paced Summaries**:
   - *Turn 1*: Generates a 50-word initial objective card (`Updating live...`).
   - *Mid-debate*: Maintains a concise 300-word rolling digest.
   - *On Completion*: Emits a high-density 500-word strategic synthesis (`Completed ✅`) with Core Objective, Perspectives, Consensus, Risks, and Action Items.
3. **Decoupled Output**: Emits a typed `DiscussionBrief` data model, leaving display presentation to the transport adapter (Slack in-place card update, CLI Markdown file, or Web UI).
