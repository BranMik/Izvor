---
name: document_reader
display_name: Document Reader
description: Read and analyze reference documents, reports, guidelines, PDF files, text files, and notes inside the resources directory to answer questions.
scope: orchestrator
triggers:
  slash:
    - /read_file
    - /read-file
    - /file
    - /attachment
    - /document_reader
  arg_type: string
tools:
  - list_documents
  - read_document
---

# Document Reader Skill

## Purpose & When to Use
Use this skill whenever you need to read, inspect, summarize, or extract information from documents, notes, guidelines, PDF files, or text assets stored in the local `resources/` directory.

## Capabilities & Tool Signatures
- `list_documents(rel_path: str = ".")`: Lists files and documents inside the `resources/` directory.
- `read_document(rel_path: str)`: Reads text or extracts pages from PDF documents inside `resources/`.

## Operational Guidelines
1. **Explore available documents**: Use `list_documents` if the exact document name or file extension is unknown.
2. **Relative to resources**: Pass paths relative to the `resources/` folder (e.g. `guide.md`, `policies.txt`).
3. **Accurate citation**: Quote or reference relevant sections of the document faithfully when answering questions.
