---
name: document_authoring
display_name: Document Authoring
description: Create, write, or update non-code documents, reports, meeting notes, and summaries inside the default agents_resources directory.
scope: agent

triggers:
  slash:
    - /document_authoring
    - /authoring
    - /write_doc
  auto_detect: true
  param_name: target_file
  handles_files: false
extraction_rule: "If user asks to draft, write, or save a document/file locally (e.g. to agents_resources/), include action '{name}' with param 'target_file'."
tools:
  - write_document
requires_human_ITL: true
---

# Document Authoring Skill

## Purpose & When to Use
Use this skill when tasked with drafting, writing, or updating non-code documents, reports, summaries, documentation, or markdown notes in the local `agents_resources/` directory.

## Capabilities & Tool Signatures
- `write_document(rel_path: str, content: str)`: Creates or completely overwrites a text document at the given relative path within the `agents_resources/` directory.

## Operational Guidelines
1. **Default working directory**: The `agents_resources/` directory is the default destination for all documents. Even if the user prompt provides only a file name (e.g. `summary.md` or `notes.txt`) without specifying the `agents_resources/` folder, always write and save it into the `agents_resources/` directory.
2. **Clear structure**: Format documents with clear Markdown headings, bullet points, and clean typography.
3. **Relative to agents_resources**: Always write paths relative to the `agents_resources/` folder (e.g. `summary.md`, `meeting_notes.txt`).
4. **Completeness**: Provide complete and polished document content without placeholder text unless explicitly requested.
5. **Permission awareness**: If an agent attempts to author or save documents without having the `document_authoring` skill assigned or permitted, they must report that they lack the required skill/permission rather than claiming the resource was not found.
