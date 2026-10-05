---
name: document_reader
display_name: Document Reader
description: Read and analyze reference documents, reports, guidelines, PDF files, text files, and notes inside the default resources directory to answer questions.
scope: agent
triggers:
  auto_detect: true
  slash:
    - /read_file
    - /read-file
    - /file
    - /attachment
    - /document_reader
  arg_type: file_target
  param_name: target_file
  handles_files: true
extraction_rule: "If user asks to read, analyze, inspect, or reference an attached Slack file or local resource file (distinct from internal documentation or knowledge base), include action '{name}' with param 'target_file' (or null if generic)."
tools:
  - list_documents
  - read_document
requires_human_ITL: false
---

# Document Reader Skill

## Purpose & When to Use
Use this skill whenever you need to read, inspect, summarize, or extract information from documents, notes, guidelines, PDF files, or text assets stored in the local `resources/` directory or attached to Slack threads.

## Capabilities & Tool Signatures
- `list_documents(rel_path: str = ".")`: Lists files and documents inside the `resources/` directory.
- `read_document(rel_path: str)`: Reads text or extracts pages from PDF documents inside `resources/`.

## Supported File Types
- **Text**: `.txt`, `.md`, `.csv`, `.json`, `.py`, `.js`, `.yaml`, `.xml`, `.html`, and other UTF-8 text files.
- **PDF**: `.pdf` — pages are extracted individually via `pypdf`.

## Operational Guidelines
1. **Default search location**: The `resources/` directory is the default search location for all documents. Even if the user prompt provides only a file name (e.g. `report.txt` or `guide.pdf`) without specifying the `resources/` folder, always locate and read it from `resources/`.
2. **Explore available documents**: Use `list_documents` if the exact document name or file extension is unknown.
3. **Relative to resources**: Pass paths relative to the `resources/` folder (e.g. `guide.md`, `policies.txt`).
4. **Accurate citation**: Quote or reference relevant sections of the document faithfully when answering questions.
5. **Permission awareness**: If an agent attempts to access or read documents without having the `document_reader` skill assigned or permitted, they must report that they lack the required skill/permission rather than claiming the file was not found.
6. **File size limit**: Files exceeding the configured file size limit (default 50 MB) will be rejected. Inform the user if a file exceeds this limit.
