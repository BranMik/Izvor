---
name: document_authoring
display_name: Document Authoring
description: Create, write, or update non-code documents, reports, meeting notes, and summaries inside the resources directory.
tools:
  - write_document
---

# Document Authoring Skill

## Purpose & When to Use
Use this skill when tasked with drafting, writing, or updating non-code documents, reports, summaries, documentation, or markdown notes in the local `resources/` directory.

## Capabilities & Tool Signatures
- `write_document(rel_path: str, content: str)`: Creates or completely overwrites a text document at the given relative path within the `resources/` directory.

## Operational Guidelines
1. **Clear structure**: Format documents with clear Markdown headings, bullet points, and clean typography.
2. **Relative to resources**: Always write paths relative to the `resources/` folder (e.g. `summary.md`, `meeting_notes.txt`).
3. **Completeness**: Provide complete and polished document content without placeholder text unless explicitly requested.
