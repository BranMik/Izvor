---
name: workspace_authoring
display_name: Workspace Authoring
description: Create or overwrite files inside the project workspace for code generation, scaffolding, and refactoring.
tools:
  - write_file
---

# Workspace Authoring Skill

## Purpose & When to Use
Use this skill when tasked with generating code, writing tests, creating documentation files, or implementing architectural modifications.

## Capabilities & Tool Signatures
- `write_file(rel_path: str, content: str)`: Creates or completely overwrites a text file at the given relative path within the workspace, automatically creating parent folders if needed.

## Operational Guidelines
1. **Be accurate and complete**: Write complete, working code without leaving dummy placeholder comments unless explicitly intended.
2. **Preserve conventions**: Match the coding style, imports, and docstring formats of existing files.
3. **Verify paths**: Ensure relative paths point to the correct directory location before writing.
