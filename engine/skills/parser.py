"""Declarative SKILL.md parser, data model, and progressive disclosure schema."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

import yaml

from services.local_tools import LOCAL_TOOLS_SCHEMAS

logger = logging.getLogger(__name__)

# Function schema for autonomous progressive disclosure
LOAD_SKILL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load an available skill by name on demand. Returns the skill's operational "
            "guidelines and unlocks its specialized tools for subsequent steps in this turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Name of the skill to load (e.g. 'web_research', 'document_reader', 'knowledge_base_rag')."
                    ),
                }
            },
            "required": ["skill_name"],
        },
    },
}


class SkillIntegrityError(ValueError):
    """Raised when a SKILL.md file violates mandatory schema requirements."""


@dataclass
class SkillDefinition:
    """Represents a declarative Skill parsed from a SKILL.md file."""

    name: str
    display_name: str
    description: str
    tools: list[str] = field(default_factory=list)
    instructions: str = ""
    skill_dir: Path | None = None
    scope: str = "agent"  # "agent" | "prompt_modifier" | "orchestrator"
    triggers: dict[str, Any] = field(default_factory=dict)
    action_handler: Callable[..., Any] | None = None
    is_global: bool = False
    is_terminal: bool = False
    requires_human_ITL: bool = False
    requires_admin: bool = False
    extraction_rule: str = ""
    handles_files: bool = False
    custom_tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    tool_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    mcp_server: str = ""
    mcp_tools: list[str] = field(default_factory=list)
    content_hash: str = ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Returns the LiteLLM tool schemas for the tools declared by this skill."""
        matched: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        if self.custom_tool_schemas:
            for schema in self.custom_tool_schemas:
                fn_name = schema.get("function", {}).get("name")
                if fn_name and fn_name not in seen_names:
                    matched.append(schema)
                    seen_names.add(fn_name)
                elif not fn_name and schema not in matched:
                    matched.append(schema)
        for schema in LOCAL_TOOLS_SCHEMAS:
            fn_name = schema.get("function", {}).get("name")
            if fn_name in self.tools and fn_name not in seen_names:
                matched.append(schema)
                seen_names.add(fn_name)

        if self.mcp_server:
            try:
                from services.mcp.client_manager import get_mcp_client_manager

                mgr = get_mcp_client_manager()
                allowed = self.mcp_tools if (self.mcp_tools and "*" not in self.mcp_tools) else None
                cached_tools = mgr.get_cached_tools_for_server(self.mcp_server, allowed_raw_names=allowed)
                for mcp_tool in cached_tools:
                    schema = mcp_tool.litellm_schema
                    fn_name = schema.get("function", {}).get("name")
                    if fn_name and fn_name not in seen_names:
                        matched.append(schema)
                        seen_names.add(fn_name)
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as e:
                logger.warning(f"[SKILLS] Failed to retrieve MCP schemas for server '{self.mcp_server}': {e}")

        return matched


def parse_skill_file(file_path: Path) -> SkillDefinition | None:
    """Parses a single SKILL.md manifest alongside optional action.py and tools.py handlers."""
    raw_text = file_path.read_text(encoding="utf-8")
    if not raw_text.startswith("---"):
        logger.warning(f"[SKILLS] Missing YAML frontmatter in {file_path}")
        return None

    parts = raw_text.split("---", 2)
    if len(parts) < 3:
        logger.warning(f"[SKILLS] Malformed frontmatter in {file_path}")
        return None

    frontmatter_str = parts[1]
    body_markdown = parts[2].strip()
    content_hash = hashlib.sha256(frontmatter_str.strip().encode("utf-8")).hexdigest()

    meta = yaml.safe_load(frontmatter_str) or {}

    required_fields = ["name", "display_name", "description", "scope", "requires_human_ITL"]
    for field_name in required_fields:
        if field_name not in meta:
            logger.warning(f"[SKILLS] Validation warning in {file_path}: missing required field '{field_name}'")

    triggers = meta.get("triggers") or {}
    if isinstance(triggers, dict) and "auto_detect" not in triggers:
        logger.warning(
            f"[SKILLS] Validation warning in {file_path}: missing field 'auto_detect' in triggers (defaulting to False - privileged/slash-only)"
        )

    name = str(meta.get("name", file_path.parent.name)).strip()
    display_name = str(meta.get("display_name", name.replace("_", " ").title())).strip()
    description = str(meta.get("description", "")).strip()
    scope = str(meta.get("scope", "agent")).strip().lower()
    if not isinstance(triggers, dict):
        triggers = {}

    raw_tools = meta.get("tools", [])
    tools = [str(t).strip() for t in raw_tools if str(t).strip()]

    mcp_server = str(meta.get("mcp_server", "")).strip()
    raw_mcp_tools = meta.get("mcp_tools") or []
    mcp_tools = [str(t).strip() for t in raw_mcp_tools if str(t).strip()] if isinstance(raw_mcp_tools, list) else []

    if mcp_server and mcp_tools:
        from services.mcp.schema_translator import format_mcp_tool_name

        for raw_t in mcp_tools:
            if raw_t != "*":
                ns_name = format_mcp_tool_name(mcp_server, raw_t)
                if ns_name not in tools:
                    tools.append(ns_name)

    is_global = bool(meta.get("global", False))
    requires_human_ITL = bool(meta.get("requires_human_ITL", False))

    # Validate mandatory requires_admin boolean (fail-closed)
    if "requires_admin" not in meta:
        raise SkillIntegrityError(
            f"[SKILLS INTEGRITY ERROR] {file_path} is missing mandatory boolean 'requires_admin' in YAML frontmatter."
        )
    if not isinstance(meta.get("requires_admin"), bool):
        raise SkillIntegrityError(
            f"[SKILLS INTEGRITY ERROR] {file_path} field 'requires_admin' must be a boolean (true/false), got {type(meta.get('requires_admin')).__name__}."
        )
    requires_admin = bool(meta.get("requires_admin"))

    action_handler = None
    action_file = file_path.parent / "action.py"
    if action_file.exists():
        try:
            mod_name = f"skills.{name}.action"
            spec = importlib.util.spec_from_file_location(mod_name, action_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "run"):
                    action_handler = mod.run
        except Exception as e:  # noqa: BLE001 - Isolate arbitrary third-party plugin load errors
            logger.error(f"[SKILLS] Error loading action handler from {action_file}: {e}")

    extraction_rule = str(meta.get("extraction_rule", "")).strip()
    if "{name}" in extraction_rule:
        extraction_rule = extraction_rule.replace("{name}", name)
    handles_files = bool(triggers.get("handles_files", False))
    is_terminal = bool(triggers.get("terminal", False))

    custom_tool_schemas: list[dict[str, Any]] = []
    tool_handlers: dict[str, Callable[..., Any]] = {}
    tools_file = file_path.parent / "tools.py"
    if tools_file.exists():
        try:
            mod_name = f"skills.{name}.tools"
            spec = importlib.util.spec_from_file_location(mod_name, tools_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "TOOL_SCHEMAS") and isinstance(mod.TOOL_SCHEMAS, list):
                    custom_tool_schemas.extend(mod.TOOL_SCHEMAS)
                if hasattr(mod, "TOOL_HANDLERS") and isinstance(mod.TOOL_HANDLERS, dict):
                    tool_handlers.update(mod.TOOL_HANDLERS)
        except Exception as e:  # noqa: BLE001 - Isolate arbitrary third-party custom tool load errors
            logger.error(f"[SKILLS] Error loading custom tools from {tools_file}: {e}")

    return SkillDefinition(
        name=name,
        display_name=display_name,
        description=description,
        tools=tools,
        instructions=body_markdown,
        skill_dir=file_path.parent,
        scope=scope,
        triggers=triggers,
        action_handler=action_handler,
        is_global=is_global,
        is_terminal=is_terminal,
        requires_human_ITL=requires_human_ITL,
        requires_admin=requires_admin,
        extraction_rule=extraction_rule,
        handles_files=handles_files,
        custom_tool_schemas=custom_tool_schemas,
        tool_handlers=tool_handlers,
        mcp_server=mcp_server,
        mcp_tools=mcp_tools,
        content_hash=content_hash,
    )

