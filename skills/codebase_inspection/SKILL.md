---
name: codebase_inspection
display_name: Codebase Inspection
description: Explore the workspace directory layout and read file contents to inspect implementation details, verify architecture, and debug code.
tools:
  - list_directory
  - read_file
---

# Codebase Inspection Skill

## Purpose & When to Use
Use this skill when you need to inspect the current codebase, verify how a module or function is implemented, investigate directory layout, or locate bugs.

## Capabilities & Tool Signatures
- `list_directory(rel_path: str = ".")`: Lists child files and subdirectories along with sizes in bytes.
- `read_file(rel_path: str)`: Reads the full UTF-8 text content of a file within the workspace boundaries.

## Operational Guidelines
1. **Explore before reading**: If you don't know the exact file path, use `list_directory` to explore the directory tree first.
2. **Specify relative paths**: Always pass paths relative to the project root (e.g. `orchestration/turn_pipeline.py`, `data/agent_settings.json`).
3. **Analyze objectively**: When evaluating code, point to specific lines or functions rather than making vague statements.
