---
name: document_authoring
display_name: Document Authoring
description: Create, write, or update non-code documents, reports, meeting notes, and summaries inside the default resources directory.
tools:
  - write_document
---

# Document Authoring Skill

## Purpose & When to Use
Use this skill when tasked with drafting, writing, or updating non-code documents, reports, summaries, documentation, or markdown notes in the local `resources/` directory.

## Capabilities & Tool Signatures
- `write_document(rel_path: str, content: str)`: Creates or completely overwrites a text document at the given relative path within the `resources/` directory.

## Operational Guidelines
1. **Default working directory**: The `resources/` directory is the default destination for all documents. Even if the user prompt provides only a file name (e.g. `summary.md` or `notes.txt`) without specifying the `resources/` folder, always write and save it into the `resources/` directory.
2. **Clear structure**: Format documents with clear Markdown headings, bullet points, and clean typography.
3. **Relative to resources**: Always write paths relative to the `resources/` folder (e.g. `summary.md`, `meeting_notes.txt`).
4. **Completeness**: Provide complete and polished document content without placeholder text unless explicitly requested.
