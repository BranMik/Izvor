---
name: directive-parser
description: Language-agnostic extractor for user constraints, slash commands, word limits, history cutoffs, and agent exclusions.
---

# Directive Parser Skill

## Purpose
Conversational agent systems receive human instructions blending discussion topics with explicit operational constraints (e.g. *"keep it under 50 words"*, *"use last 3 messages"*, *"without the security architect"*, *"analyze schema.sql"*).

This skill provides a **two-tier parsing architecture**:
1. **Tier 1 (Fast-Path Regex & Heuristics)**: Instant (0ms) deterministic extraction of slash commands (`/history 3`, `/max_words 50`, `/exclude [Agent]`, `/stop`) and common multilingual natural language idioms (English & Croatian).
2. **Tier 2 (Cognitive Micro-LLM Fallback)**: Intelligent semantic extraction for colloquial, complex, or foreign-language phrasing when fast-path heuristics are ambiguous and thread attachments exist.
