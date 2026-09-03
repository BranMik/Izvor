---
name: entity-resolver
description: Normalizes, resolves, and sanitizes agent tags, aliases, and usernames with partial matching and ambiguity detection.
---

# Entity Resolver Skill

## Purpose
In a multi-agent panel debate, users and agents reference personas using bracketed tags (e.g., `[Visionary]`, `[Architect]`), short nicknames (e.g., `[Sec]`, `[Tech]`), or conversational mentions. This skill provides:
1. **Fuzzy & Partial Tag Resolution**: Resolves informal or abbreviated tags to canonical agent keys.
2. **Ambiguity Guards**: Detects if a tag matches multiple active agents (e.g. `[Strategist]` matching both `DataAndAnalyticsStrategist` and `ROIStrategist`), generating clear disambiguation feedback.
3. **Unrecognized Tag Detection**: Distinguishes valid agent tags from arbitrary bracketed text (e.g., `[Note]`, `[Update]`, `[150/50]`) to prevent accidental interruptions or false positives.
4. **Bracket Sanitization**: Strips or protects bracket syntax in outgoing agent messages.
