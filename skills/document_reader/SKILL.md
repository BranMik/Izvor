---
name: document_reader
display_name: Document Reader
description: Read and analyze reference documents, reports, guidelines, PDF files, text files, image attachments (PNG, JPEG, WEBP, GIF), and notes inside the default resources directory or attached to Slack threads.
scope: agent
triggers:
  slash:
    - /read_file
    - /read-file
    - /read-image
    - /read_image
    - /document_reader
    - /image
    - /screenshot
    - /photo
    - /diagram
  arg_type: string
tools:
  - list_documents
  - read_document
---

# Document Reader Skill

## Purpose & When to Use
Use this skill whenever you need to read, inspect, summarize, or extract information from documents, notes, guidelines, PDF files, text assets, or **image attachments** (PNG, JPEG, WEBP, GIF, BMP) stored in the local `resources/` directory or uploaded to Slack threads.

## Capabilities & Tool Signatures
- `list_documents(rel_path: str = ".")`: Lists files and documents inside the `resources/` directory.
- `read_document(rel_path: str)`: Reads text, extracts pages from PDF documents, or encodes image files as Base64 data URLs inside `resources/`.

## Supported File Types
- **Text**: `.txt`, `.md`, `.csv`, `.json`, `.py`, `.js`, `.yaml`, `.xml`, `.html`, and other UTF-8 text files.
- **PDF**: `.pdf` — pages are extracted individually via `pypdf`.
- **Images**: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp` — encoded as Base64 data URLs for multimodal (vision) LLM consumption.

## Operational Guidelines
1. **Default search location**: The `resources/` directory is the default search location for all documents. Even if the user prompt provides only a file name (e.g. `report.txt` or `diagram.png`) without specifying the `resources/` folder, always locate and read it from `resources/`.
2. **Explore available documents**: Use `list_documents` if the exact document name or file extension is unknown.
3. **Relative to resources**: Pass paths relative to the `resources/` folder (e.g. `guide.md`, `policies.txt`, `architecture.png`).
4. **Accurate citation**: Quote or reference relevant sections of the document faithfully when answering questions.
5. **Permission awareness**: If an agent attempts to access or read documents without having the `document_reader` skill assigned or permitted, they must report that they lack the required skill/permission rather than claiming the file was not found.
6. **Image analysis**: When image files are attached to a Slack thread, they are automatically passed to vision-capable models as multimodal content. Agents should analyze diagrams, screenshots, and visual assets alongside textual context.
7. **File size limit**: Files exceeding the configured file size limit (default 50 MB) will be rejected. Inform the user if a file exceeds this limit.

