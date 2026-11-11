"""
Declarative SKILL.md Loader & Progressive Disclosure Registry.
Discovers and parses SKILL.md files (YAML frontmatter + Markdown body)
and binds low-level MCP execution backends.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
from typing import Any

import yaml

from core.paths import ENGINE_SKILLS_DIR, SKILLS_DIR
from core.domain import ToolPrincipal
from services.local_tools import LOCAL_TOOLS_SCHEMAS, execute_local_tool

logger = logging.getLogger(__name__)

# Sentinel constant to allow internal/system unrestricted tool execution
ALL_INTERNAL_SKILLS = "__ALL_INTERNAL_SKILLS__"

# Function schema for autonomous progressive disclosure
LOAD_SKILL_SCHEMA = {
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
                        if fn_name not in self.tools:
                            self.tools.append(fn_name)
            except Exception as e:
                logger.warning(f"[SKILLS] Failed to retrieve MCP schemas for server '{self.mcp_server}': {e}")

        return matched


class SkillRegistry:
    """
    Registry that dynamically scans and loads all SKILL.md files from both
    engine/skills/builtin/ (system directives) and config/skills/ (agent tools).
    """

    def __init__(self, root_dir: Path | list[Path] | None = None) -> None:
        if root_dir is None:
            self.root_dirs: list[Path] = [ENGINE_SKILLS_DIR, SKILLS_DIR]
            self.root_dir = SKILLS_DIR
        elif isinstance(root_dir, (list, tuple)):
            self.root_dirs = [Path(p) for p in root_dir]
            self.root_dir = self.root_dirs[-1]
        else:
            self.root_dirs = [Path(root_dir)]
            self.root_dir = Path(root_dir)
        self._skills: dict[str, SkillDefinition] = {}
        self.reload()

    def reload(self) -> None:
        """Scans root_dirs for all subdirectories containing SKILL.md."""
        self._skills.clear()
        for r_dir in self.root_dirs:
            if not r_dir.exists():
                continue
            for skill_dir in sorted(r_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue

                try:
                    skill_def = self._parse_skill_file(skill_file)
                    if skill_def:
                        self._skills[skill_def.name] = skill_def
                        logger.debug(
                            f"[SKILLS] Loaded declarative skill '{skill_def.name}' ({skill_def.scope}) from {skill_file}"
                        )
                except SkillIntegrityError:
                    raise
                except (OSError, yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as e:
                    logger.error(f"[SKILLS] Failed to parse {skill_file}: {e}")

    def _parse_skill_file(self, file_path: Path) -> SkillDefinition | None:
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

        # Validate required fields
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
        triggers = meta.get("triggers") or {}
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

        # Check for action.py handler
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
            except Exception as e:  # noqa: BLE001 - Dynamic plugin module loading catches all module errors to isolate broken skills
                logger.error(f"[SKILLS] Error loading action handler from {action_file}: {e}")

        extraction_rule = str(meta.get("extraction_rule", "")).strip()
        if "{name}" in extraction_rule:
            extraction_rule = extraction_rule.replace("{name}", name)
        handles_files = bool(triggers.get("handles_files", False))
        is_terminal = bool(triggers.get("terminal", False))

        # Check for tools.py handler and schemas
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
            except Exception as e:  # noqa: BLE001 - Isolate skill custom tool load errors
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

    def get(self, name: str) -> SkillDefinition | None:
        """Retrieve a skill by name."""
        return self._skills.get(name)

    def resolve_skill(self, name_or_alias: str) -> SkillDefinition | None:
        """Resolves a skill by its canonical name or slash command alias (e.g. 'grant_for_all' or '/grant_for_all')."""
        if not name_or_alias:
            return None
        target = str(name_or_alias).strip().lower()
        if target in self._skills:
            return self._skills[target]
        cmd = target if target.startswith("/") else f"/{target}"
        slash_triggers = self.get_slash_triggers()
        if cmd in slash_triggers:
            return slash_triggers[cmd][0]
        bare_cmd = target.lstrip("/")
        for trigger_cmd, (sk_def, _) in slash_triggers.items():
            if trigger_cmd.lstrip("/").lower() == bare_cmd:
                return sk_def
        return None

    def get_admin_skills(self) -> list[SkillDefinition]:
        """Returns all registered skills that require admin privileges."""
        return [s for s in self._skills.values() if getattr(s, "requires_admin", False)]

    def filter_skills_for_caller(
        self,
        skill_names: list[str] | set[str] | None,
        is_admin: bool,
        thread_granted_skills: set[str] | list[str] | None = None,
    ) -> list[str]:
        """
        Filters a list of skill names based on whether the turn caller is a Slack Workspace Admin.
        If caller is not an admin, skills with requires_admin=True are stripped unless explicitly
        granted to the thread by an admin.
        """
        raw_list = list(skill_names or [])
        if is_admin:
            return raw_list
        granted = set(thread_granted_skills or ())
        filtered: list[str] = []
        for s in raw_list:
            sk = self.get(s)
            if sk and getattr(sk, "requires_admin", False) and s not in granted:
                continue
            filtered.append(s)
        return filtered

    def list_skills(self) -> list[SkillDefinition]:
        """Returns all registered skills."""
        return list(self._skills.values())

    def get_global_skills(self) -> list[str]:
        """Returns a list of names for skills marked global in SKILL.md."""
        return [s.name for s in self._skills.values() if s.is_global]

    def get_agent_skills(self, allowed_skills: list[str] | None = None) -> list[SkillDefinition]:
        """Returns skills with scope == 'agent', optionally filtered by allowed_skills and including global skills."""
        res = [s for s in self._skills.values() if s.scope == "agent"]
        if allowed_skills is not None:
            res = [s for s in res if s.name in allowed_skills or s.is_global]
        return res

    def get_slash_triggers(self) -> dict[str, tuple[SkillDefinition, dict[str, Any]]]:
        """Maps each registered slash alias (lowercase) to its (SkillDefinition, trigger_dict)."""
        mapping: dict[str, tuple[SkillDefinition, dict[str, Any]]] = {}
        for skill in self._skills.values():
            slash_list = skill.triggers.get("slash") or []
            if isinstance(slash_list, str):
                slash_list = [slash_list]
            for cmd in slash_list:
                cleaned = str(cmd).strip().lower()
                if cleaned:
                    if not cleaned.startswith("/"):
                        cleaned = f"/{cleaned}"
                    mapping[cleaned] = (skill, skill.triggers)
        return mapping

    def get_terminal_slash_triggers(self) -> list[str]:
        """Returns all slash triggers for skills configured with terminal: true."""
        terminals: list[str] = []
        for skill in self._skills.values():
            if skill.is_terminal:
                slash_list = skill.triggers.get("slash") or []
                if isinstance(slash_list, str):
                    slash_list = [slash_list]
                for cmd in slash_list:
                    cleaned = str(cmd).strip().lower()
                    if cleaned:
                        if not cleaned.startswith("/"):
                            cleaned = f"/{cleaned}"
                        terminals.append(cleaned)
        return terminals

    def is_stop_command(self, raw_text: str, is_mention_or_dm: bool = False) -> bool:
        """Detects if raw_text contains an explicit terminal/stop command."""
        if not raw_text:
            return False

        terminals = self.get_terminal_slash_triggers()
        if not terminals:
            terminals = ["/stop"]
        for cmd in terminals:
            pattern = rf"(?:^|\s){re.escape(cmd)}(?=[\s!.,;?]|$)"
            if re.search(pattern, raw_text, re.IGNORECASE):
                return True
        return False

    def get_file_handler_skill(self) -> SkillDefinition | None:
        """Returns the skill configured to handle file attachments, or None."""
        for skill in self._skills.values():
            if skill.handles_files:
                return skill
        for skill in self._skills.values():
            if "read_document" in skill.tools or "write_document" in skill.tools:
                return skill
        return None

    def get_auto_detect_skills(self) -> list[SkillDefinition]:
        """Returns all skills where auto_detect is True (defaults to False if omitted)."""
        return [s for s in self._skills.values() if s.triggers.get("auto_detect", False) is True]

    def build_extractor_catalog_prompt(self) -> str:
        """
        Dynamically generates the system prompt for the IntentExtractor LLM
        based on all registered auto-detect skills and their extraction rules.
        """
        skills = self.get_auto_detect_skills()
        prompt_lines = [
            "You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.",
            "Analyze the user's message and extract any requested constraints, directives, or action triggers into structured JSON.",
            "Also detect the natural language of the user's message (e.g. \"Croatian\", \"English\", \"German\") and report it in the \"language\" field.",
            "",
            "AVAILABLE SYSTEM SKILLS & DIRECTIVES:",
        ]

        # 1. Prompt modifiers (e.g. max_words, min_words)
        modifiers_lines = []
        actions_lines = []

        for skill in skills:
            desc = skill.description or skill.instructions[:150]
            if skill.scope == "prompt_modifier":
                arg_type = skill.triggers.get("arg_type", "string")
                modifiers_lines.append(f'- "{skill.name}" ({arg_type}): {desc}')
            else:
                actions_lines.append(f'- "{skill.name}": {desc}')

        if modifiers_lines:
            prompt_lines.append("MODIFIERS (Output constraints & styling):")
            prompt_lines.extend(modifiers_lines)
            prompt_lines.append("")

        if actions_lines:
            prompt_lines.append("ACTIONS (Operations, tools & pipeline triggers):")
            prompt_lines.extend(actions_lines)
            prompt_lines.append("")

        special_rules = []
        for skill in skills:
            if skill.extraction_rule:
                special_rules.append(f"- '{skill.name}': {skill.extraction_rule}")
        special_rules.append(
            "- 'is_pure_skill_task': Set true if the user's prompt is purely an informational request, file lookup, or action to be executed via a skill without asking the panel to debate, critique, or brainstorm broader strategic alternatives. Set false if the user asks for a conceptual debate, critique, or brainstorming alongside or without the skill."
        )

        if special_rules:
            prompt_lines.append("SPECIAL RULES:")
            prompt_lines.extend(special_rules)
            prompt_lines.append("")

        schema_modifiers = []
        for skill in skills:
            if skill.scope == "prompt_modifier":
                arg_type = skill.triggers.get("arg_type", "string")
                schema_modifiers.append(f'    "{skill.name}": <{arg_type} or null>')

        schema_lines = [
            "Respond ONLY with a valid JSON object matching this schema:",
            "{",
            '  "modifiers": {',
        ]
        if schema_modifiers:
            schema_lines.append(",\n".join(schema_modifiers))
        else:
            schema_lines.append('    "<modifier_name>": <value>')
        schema_lines.extend(
            [
                "  },",
                '  "actions": [',
                '    { "name": "<skill_name>", "params": { ... } }',
                "  ],",
                '  "language": "<detected_language_name_or_code>",',
                '  "is_pure_skill_task": <true if the prompt asks ONLY to perform the skill/file action without a broader strategic debate topic, else false>',
                "}",
            ]
        )
        prompt_lines.extend(schema_lines)
        return "\n".join(prompt_lines)

    def get_tools_for_skills(self, skill_names: list[str] | None) -> list[dict[str, Any]]:
        """Aggregates tool schemas for a list of skill names without duplicates."""
        if not skill_names:
            return []

        tools: list[dict[str, Any]] = []
        seen: set[str] = set()

        for s_name in skill_names:
            skill = self._skills.get(s_name)
            if not skill:
                continue
            for schema in skill.get_tool_schemas():
                fn_name = schema.get("function", {}).get("name")
                if fn_name and fn_name not in seen:
                    tools.append(schema)
                    seen.add(fn_name)

        return tools

    def find_skill_for_tool(self, tool_name: str) -> str | None:
        """Finds the skill name that defines or provides the given tool_name."""
        for s_name, s_def in self._skills.items():
            if tool_name in s_def.tools:
                return s_name
            for schema in s_def.custom_tool_schemas:
                if schema.get("function", {}).get("name") == tool_name:
                    return s_name
        return None

    def get_skills_catalog_prompt(self, allowed_skills: list[str] | None = None) -> str:
        """
        Generates the compact skills catalog for injection into the system prompt.
        If allowed_skills is provided, only lists skills the agent is authorized to load.
        Returns empty string if agent is authorized for 0 skills.
        """
        if not self._skills:
            return ""

        # Strictly only show agent-scoped skills to LLM agents
        skills_to_show = [s for s in self._skills.values() if s.scope == "agent"]
        if allowed_skills is not None:
            skills_to_show = [s for s in skills_to_show if s.name in allowed_skills or s.is_global]
            if not skills_to_show:
                return ""

        lines = [
            "### AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)",
            (
                "You have access to specialized capabilities on demand. If your task requires live data, "
                "repository inspection, or file authoring, invoke the `load_skill(skill_name)` tool:"
            ),
        ]
        for skill in skills_to_show:
            lines.append(f"- `{skill.name}`: {skill.description}")

        lines.extend(
            [
                "",
                "SKILL INFERENCE RULE:",
                "- If your turn is purely conversational do NOT load any skills—respond directly.",
                "- If you need external facts or file access, call `load_skill` with the relevant skill name.",
            ]
        )
        return "\n".join(lines) + "\n"

    def get_skill_agent_mapping(
        self, channel_id: str | None = None, thread_ts: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """
        Returns a dictionary mapping each registered skill name to its metadata and authorized agents.
        """
        from core.config import AGENTS_CONFIG

        try:
            from orchestration.agent_settings import settings_manager

            agents_cfg = settings_manager.agents_config or AGENTS_CONFIG
        except (ImportError, AttributeError, TypeError):
            agents_cfg = AGENTS_CONFIG

        thread_skills: list[str] = []
        thread_agent_skills: dict[str, list[str]] = {}
        if channel_id and thread_ts:
            try:
                from orchestration.thread_skills_manager import thread_skills_manager

                thread_skills = thread_skills_manager.get_active_skills(channel_id, thread_ts)
                thread_agent_skills = thread_skills_manager.get_all_active_agent_skills(channel_id, thread_ts)
            except (ImportError, AttributeError, KeyError, TypeError):
                thread_skills = []
                thread_agent_skills = {}

        mapping: dict[str, dict[str, Any]] = {}
        for skill in self.list_skills():
            auth_agents: list[str] = []
            if skill.is_global:
                auth_agents = ["All Agents (Global)"]
            elif skill.name in thread_skills:
                auth_agents = ["All Agents (Thread-Global Active)"]
            elif skill.scope == "agent":
                matched = [
                    agent_key
                    for agent_key, cfg in sorted(agents_cfg.items())
                    if isinstance(cfg, dict) and skill.name in cfg.get("skills", [])
                ]
                for ag_key, ag_s_list in sorted(thread_agent_skills.items()):
                    if skill.name in ag_s_list and ag_key not in matched:
                        matched.append(f"{ag_key} (Thread Active)")
                auth_agents = matched if matched else ["No agents currently assigned"]
            elif skill.scope == "orchestrator":
                auth_agents = ["System / Orchestrator Directive"]
            elif skill.scope == "prompt_modifier":
                auth_agents = ["System / Prompt Modifier"]
            else:
                auth_agents = ["System"]

            mapping[skill.name] = {
                "display_name": skill.display_name,
                "description": skill.description,
                "scope": skill.scope,
                "tools": skill.tools,
                "is_global": skill.is_global,
                "requires_admin": getattr(skill, "requires_admin", False),
                "authorized_agents": auth_agents,
            }
        return mapping

    def format_skills_directory_markdown(self, channel_id: str | None = None, thread_ts: str | None = None) -> str:
        """
        Formats the full platform skills directory into clean markdown.
        Beside each skill is listed all agents who can use it.
        """
        mapping = self.get_skill_agent_mapping(channel_id=channel_id, thread_ts=thread_ts)
        lines = ["*Application Skills & Capabilities Directory*:\n"]

        agent_skills = []
        directive_skills = []
        modifier_skills = []

        for name, info in sorted(mapping.items()):
            agents_str = ", ".join(info["authorized_agents"])
            desc = info["description"] or "No description provided."
            admin_badge = " (🔒 *Admin Only*)" if info.get("requires_admin") else ""
            entry = f"• `{name}`{admin_badge}: {desc}\n   ↳ *Authorized*: {agents_str}"
            if info["scope"] == "agent":
                agent_skills.append(entry)
            elif info["scope"] == "prompt_modifier":
                modifier_skills.append(entry)
            else:
                directive_skills.append(entry)

        if agent_skills:
            lines.append("*Agent Skills (Tools & Capabilities)*:")
            lines.extend(agent_skills)
            lines.append("")

        if directive_skills:
            lines.append("*Orchestrator Directives & Control Skills*:")
            lines.extend(directive_skills)
            lines.append("")

        if modifier_skills:
            lines.append("*Prompt & Output Modifiers*:")
            lines.extend(modifier_skills)
            lines.append("")

        lines.append(
            "_Note: Agents are strictly limited to their assigned permissions and cannot execute unauthorized skills, unless /grant_skill_for_all instruction was given in a thread._"
        )
        return "\n".join(lines)

    def get_skill_resolution_hint(
        self,
        skill_names: list[str] | str,
        channel_id: str | None = None,
        thread_ts: str | None = None,
    ) -> str:
        """
        Generates a concise 1-line resolution hint naming authorized agents (if any)
        and how to grant the skill via /grant_skill or /grant_skill_for_all.
        """
        if isinstance(skill_names, str):
            skill_list = [skill_names.strip()]
        else:
            skill_list = [str(s).strip() for s in skill_names if str(s).strip()]

        if not skill_list:
            return "Grant access via '/grant_skill <skill> <agent>' or '/grant_skill_for_all <skill>'."

        mapping = self.get_skill_agent_mapping(channel_id=channel_id, thread_ts=thread_ts)
        hints = []
        for s_name in skill_list:
            info = mapping.get(s_name)
            raw_auth = info.get("authorized_agents", []) if info else []
            auth_agents = [
                a for a in raw_auth
                if not a.startswith("All Agents") and not a.startswith("System") and not a.startswith("No agents")
            ]
            if auth_agents:
                agents_str = ", ".join(auth_agents)
                hints.append(
                    f"Authorized for '{s_name}': {agents_str} — or grant via '/grant_skill {s_name} <agent>' or '/grant_skill_for_all {s_name}'."
                )
            else:
                hints.append(
                    f"Grant '{s_name}' via '/grant_skill {s_name} <agent>' or '/grant_skill_for_all {s_name}'."
                )
        return " ".join(hints)

    def get_skills_directory_prompt(
        self, channel_id: str | None = None, thread_ts: str | None = None
    ) -> str:
        """
        Generates the comprehensive application skills directory for injection into agent system prompts.
        Informs all agents of platform skills and which agents can use them, while enforcing boundary awareness.
        Reflects active thread-level skill grants when channel_id and thread_ts are provided.
        """
        mapping = self.get_skill_agent_mapping(channel_id=channel_id, thread_ts=thread_ts)
        lines = [
            "### APPLICATION SKILLS DIRECTORY",
            "The platform provides the following skills. Beside each skill is the list of agents authorized to use it:",
        ]
        for name, info in sorted(mapping.items()):
            agents_str = ", ".join(info["authorized_agents"])
            lines.append(f"- `{name}`: {info['description']} (Authorized: {agents_str})")

        lines.extend(
            [
                "",
                "PERMISSION BOUNDARIES & USAGE RESTRICTIONS:",
                "- Reference this directory to answer user inquiries regarding platform capabilities and agent skill assignments.",
                "- Agents are strictly limited in tool usage: load and execute tools ONLY for skills explicitly authorized for your agent role, granted thread-actively, or listed under '### AVAILABLE SKILLS'.",
                "- Any skill marked with '(Authorized: All Agents (Thread-Global Active))', '(Thread Active)', or listed under '### AVAILABLE SKILLS' IS authorized for you to use in this thread.",
                "- When a user requests an action requiring an unauthorized skill that is not authorized for you in this thread, explicitly report lack of permission in at most 50 words (including a 1-line hint naming an authorized agent or /grant_skill) without reflecting on skills architecture, and proceed normally with any other parts of the prompt that do not require the missing skill.",
                "- Never attempt to circumvent permission boundaries.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _verify_tool_authorization(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        authorized_skills: list[str] | str | None = None,
        expired_skills: list[str] | None = None,
        ttl_hours: float | None = None,
        agent_name: Any = "",
    ) -> tuple[bool, str | None, list[str]]:
        """
        Unified tool and skill authorization check with audit event logging (A-17).
        Returns: (is_authorized, early_response_or_None, effective_authorized_skills).
        """
        is_system_principal = isinstance(agent_name, ToolPrincipal) and agent_name.kind == "system"
        is_internal = (
            is_system_principal
            or authorized_skills == ALL_INTERNAL_SKILLS
            or (isinstance(authorized_skills, (list, tuple, set)) and ALL_INTERNAL_SKILLS in authorized_skills)
        )
        actor_id = agent_name.id if hasattr(agent_name, "id") else str(agent_name or "unknown_agent")
        actor_kind = getattr(agent_name, "kind", "agent")

        normalized_auth = normalize_skills_list(authorized_skills)
        effective_authorized_skills: list[str] = (
            [s.name for s in self._skills.values()] if is_internal else normalized_auth
        )

        if tool_name == "load_skill":
            skill_name = arguments.get("skill_name", "").strip()
            target_skill = self.get(skill_name)
            is_globally_allowed = bool(target_skill and target_skill.is_global)

            if not is_internal and not is_globally_allowed and skill_name not in effective_authorized_skills:
                from services.audit_logger import log_audit_event

                if expired_skills and skill_name in expired_skills:
                    ttl_str = f"{ttl_hours:.1f} hours" if ttl_hours else "the expiration limit"
                    log_audit_event(
                        event_type="SECURITY_POLICY_VIOLATION",
                        actor={"type": actor_kind, "id": actor_id},
                        action="load_skill",
                        target=f"skill:{skill_name}",
                        details={"reason": "Skill expired", "ttl_hours": ttl_hours},
                        severity="CRITICAL",
                        status="BLOCKED",
                    )
                    return (
                        False,
                        json.dumps(
                            {
                                "error": (
                                    f"Permission denied: Thread global skill '{skill_name}' expired after {ttl_str}. "
                                    f"Run `/grant_skill_for_all {skill_name}` in this thread to grant access again."
                                )
                            }
                        ),
                        effective_authorized_skills,
                    )
                allowed_str = ", ".join(f"'{s}'" for s in effective_authorized_skills) or "none"
                hint_str = self.get_skill_resolution_hint(skill_name)
                log_audit_event(
                    event_type="SECURITY_POLICY_VIOLATION",
                    actor={"type": actor_kind, "id": actor_id},
                    action="load_skill",
                    target=f"skill:{skill_name}",
                    details={
                        "reason": "Agent not authorized for skill",
                        "authorized_skills": effective_authorized_skills,
                    },
                    severity="CRITICAL",
                    status="BLOCKED",
                )
                return (
                    False,
                    json.dumps(
                        {
                            "error": (
                                f"Permission denied: Agent is not authorized to load skill '{skill_name}'. "
                                f"Permitted skills: {allowed_str}. "
                                f"CRITICAL INSTRUCTION: State in at most 50 words that permission / the required skill '{skill_name}' is lacking, "
                                f"including this 1-line hint: {hint_str} "
                                f"Do NOT report that the file or resource was not found. Do NOT reflect on skills architecture. "
                                f"If the prompt contains other assessable parts not requiring '{skill_name}', proceed normally with those; otherwise stop."
                            )
                        }
                    ),
                    effective_authorized_skills,
                )

            skill = target_skill
            if not skill:
                avail = ", ".join(f"'{s.name}'" for s in self.list_skills())
                return (
                    False,
                    json.dumps({"error": f"Skill '{skill_name}' not found. Available skills: {avail}"}),
                    effective_authorized_skills,
                )

            if skill.mcp_server:
                try:
                    from services.mcp.client_manager import get_mcp_client_manager
                    mgr = get_mcp_client_manager()
                    allowed = skill.mcp_tools if (skill.mcp_tools and "*" not in skill.mcp_tools) else None
                    cached = mgr.get_cached_tools_for_server(skill.mcp_server, allowed_raw_names=allowed)
                    for t in cached:
                        if t.namespaced_name not in skill.tools:
                            skill.tools.append(t.namespaced_name)
                except Exception as e:
                    logger.debug(f"[SKILLS] Error updating MCP tools for skill {skill.name}: {e}")

            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="SKILL_LOADED",
                actor={"type": actor_kind, "id": actor_id},
                action="load_skill",
                target=f"skill:{skill.name}",
                details={"unlocked_tools": skill.tools},
                severity="INFO",
                status="SUCCESS",
            )

            return (
                True,
                json.dumps(
                    {
                        "status": "loaded",
                        "skill": skill.name,
                        "display_name": skill.display_name,
                        "unlocked_tools": skill.tools,
                        "operational_guidelines": skill.instructions,
                    },
                    indent=2,
                ),
                effective_authorized_skills,
            )

        # For any other tool, verify authorization against authorized/global skills (fail-closed)
        if not is_internal:
            permitted_tools = get_permitted_tools_for_principal(
                principal=agent_name,
                authorized_skills=effective_authorized_skills,
                skill_registry=self,
            )

            if tool_name not in permitted_tools:
                allowed_tools_str = ", ".join(f"'{t}'" for t in sorted(permitted_tools)) or "none"
                s_name = self.find_skill_for_tool(tool_name) or tool_name
                hint_str = self.get_skill_resolution_hint(s_name)
                from services.audit_logger import log_audit_event

                log_audit_event(
                    event_type="SECURITY_POLICY_VIOLATION",
                    actor={"type": actor_kind, "id": actor_id},
                    action="execute_tool",
                    target=f"tool:{tool_name}",
                    details={
                        "reason": "Agent not authorized for tool",
                        "permitted_tools": sorted(permitted_tools),
                    },
                    severity="CRITICAL",
                    status="BLOCKED",
                )
                return (
                    False,
                    json.dumps(
                        {
                            "error": (
                                f"Permission denied: Agent is not authorized to execute tool '{tool_name}'. "
                                f"Permitted tools: {allowed_tools_str}. "
                                f"CRITICAL INSTRUCTION: State in at most 50 words that permission to execute tool '{tool_name}' is lacking, "
                                f"including this 1-line hint: {hint_str} "
                                f"Do NOT report that the file or resource was not found. Do NOT reflect on skills architecture. "
                                f"If the prompt contains other assessable parts not requiring '{tool_name}', proceed normally with those; otherwise stop."
                            )
                        }
                    ),
                    effective_authorized_skills,
                )

        return True, None, effective_authorized_skills

    async def execute_tool_async(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        authorized_skills: list[str] | str | None = None,
        expired_skills: list[str] | None = None,
        ttl_hours: float | None = None,
        agent_name: Any = "",
    ) -> str:
        """
        Asynchronously dispatches tool execution with fail-closed authorization.
        Directly awaits coroutines, MCP tools, and native async local tools.
        Offloads synchronous custom tool handlers to asyncio.to_thread.
        """
        is_ok, early_resp, effective_authorized_skills = self._verify_tool_authorization(
            tool_name=tool_name,
            arguments=arguments,
            authorized_skills=authorized_skills,
            expired_skills=expired_skills,
            ttl_hours=ttl_hours,
            agent_name=agent_name,
        )
        if not is_ok or early_resp is not None:
            return early_resp

        # Check if MCP tool
        from services.mcp.schema_translator import is_mcp_namespaced_tool

        if is_mcp_namespaced_tool(tool_name):
            from services.mcp.security_bridge import execute_external_mcp_tool

            return await execute_external_mcp_tool(
                namespaced_tool_name=tool_name,
                arguments=arguments,
                agent_name=agent_name,
                authorized_skills=effective_authorized_skills,
                skill_registry=self,
            )

        # Check if any skill registered a custom tool handler
        for s_def in self._skills.values():
            if tool_name in s_def.tool_handlers:
                handler = s_def.tool_handlers[tool_name]
                try:
                    actor_id = (
                        agent_name.id
                        if isinstance(agent_name, ToolPrincipal)
                        else (str(agent_name) if agent_name is not None else "")
                    )
                    async_fn = getattr(handler, "_async_fn", handler)
                    sig = inspect.signature(async_fn)
                    kwargs = {"agent_name": actor_id} if "agent_name" in sig.parameters else {}
                    if inspect.iscoroutinefunction(async_fn):
                        return await async_fn(arguments, **kwargs)
                    else:
                        return await asyncio.to_thread(handler, arguments, **kwargs)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[SKILLS] Custom tool handler error for '{tool_name}': {e}")
                    return json.dumps({"error": f"Tool execution failed: {e}"})

        return await execute_local_tool(
            tool_name,
            arguments,
            agent_name=agent_name,
            authorized_skills=list(effective_authorized_skills),
        )


def normalize_skills_list(skills: list[str] | set[str] | str | None) -> list[str]:
    """Safely normalizes authorized skills, avoiding character slicing on strings."""
    if skills is None:
        return []
    if isinstance(skills, str):
        return [skills]
    return list(skills)


def get_permitted_tools_for_principal(
    principal: Any,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> set[str]:
    """Returns the complete set of permitted tools for a given principal and skills."""
    from core.domain import ToolPrincipal

    if skill_registry is None:
        skill_registry = get_skill_registry()

    is_system_principal = isinstance(principal, ToolPrincipal) and principal.kind == "system"
    is_internal = (
        is_system_principal
        or authorized_skills == ALL_INTERNAL_SKILLS
        or (isinstance(authorized_skills, (list, tuple, set)) and ALL_INTERNAL_SKILLS in authorized_skills)
    )

    if is_internal:
        all_tools: set[str] = set()
        skills = (
            skill_registry.list_skills()
            if hasattr(skill_registry, "list_skills")
            else list(skill_registry._skills.values())
        )
        for s in skills:
            all_tools.update(getattr(s, "tools", []))
            for schema in getattr(s, "custom_tool_schemas", []):
                fn = schema.get("function", {}).get("name")
                if fn:
                    all_tools.add(fn)
        return all_tools

    if isinstance(principal, ToolPrincipal):
        actor_id = (principal.id or "").strip()
    elif isinstance(principal, str):
        actor_id = principal.strip()
    else:
        actor_id = ""

    skills_list = normalize_skills_list(authorized_skills)

    static_skills: list[str] = []
    if authorized_skills is None and actor_id and actor_id != "system":
        try:
            from orchestration.agent_settings import get_agent_skills

            static_skills = get_agent_skills(actor_id) or []
        except Exception:
            pass

    combined_skills = set(static_skills) | set(skills_list)
    permitted: set[str] = set()

    for s_name in combined_skills:
        s_def = skill_registry.get(s_name) if hasattr(skill_registry, "get") else None
        if s_def:
            permitted.update(getattr(s_def, "tools", []))
            for schema in getattr(s_def, "custom_tool_schemas", []):
                fn = schema.get("function", {}).get("name")
                if fn:
                    permitted.add(fn)
            if getattr(s_def, "mcp_server", ""):
                try:
                    from services.mcp.client_manager import get_mcp_client_manager
                    mgr = get_mcp_client_manager()
                    allowed = getattr(s_def, "mcp_tools", None)
                    allowed_raw = allowed if (allowed and "*" not in allowed) else None
                    cached = mgr.get_cached_tools_for_server(s_def.mcp_server, allowed_raw_names=allowed_raw)
                    for t in cached:
                        permitted.add(t.namespaced_name)
                except Exception:
                    pass

    skills_iter = (
        skill_registry.list_skills()
        if hasattr(skill_registry, "list_skills")
        else list(skill_registry._skills.values())
    )
    for s_def in skills_iter:
        if getattr(s_def, "is_global", False):
            if getattr(s_def, "requires_admin", False) and s_def.name not in combined_skills:
                continue
            permitted.update(getattr(s_def, "tools", []))
            for schema in getattr(s_def, "custom_tool_schemas", []):
                fn = schema.get("function", {}).get("name")
                if fn:
                    permitted.add(fn)
            if getattr(s_def, "mcp_server", ""):
                try:
                    from services.mcp.client_manager import get_mcp_client_manager
                    mgr = get_mcp_client_manager()
                    allowed = getattr(s_def, "mcp_tools", None)
                    allowed_raw = allowed if (allowed and "*" not in allowed) else None
                    cached = mgr.get_cached_tools_for_server(s_def.mcp_server, allowed_raw_names=allowed_raw)
                    for t in cached:
                        permitted.add(t.namespaced_name)
                except Exception:
                    pass

    return permitted


def is_principal_authorized_for_tool(
    principal: Any,
    tool_name: str,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> bool:
    """Validates whether a principal has authorization to execute a tool (fail-closed)."""
    from core.domain import ToolPrincipal

    if isinstance(principal, ToolPrincipal) and principal.kind == "system":
        return True

    is_internal = authorized_skills == ALL_INTERNAL_SKILLS or (
        isinstance(authorized_skills, (list, tuple, set)) and ALL_INTERNAL_SKILLS in authorized_skills
    )
    if is_internal:
        return True

    if isinstance(principal, ToolPrincipal):
        actor_id = (principal.id or "").strip()
    elif isinstance(principal, str):
        actor_id = principal.strip()
    else:
        actor_id = ""

    # Raw string "system" or empty string does NOT bypass authorization
    if not actor_id or actor_id == "system":
        return False

    permitted = get_permitted_tools_for_principal(
        principal=principal,
        authorized_skills=authorized_skills,
        skill_registry=skill_registry,
    )
    return tool_name in permitted


_skill_registry_instance: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """Returns the canonical global SkillRegistry singleton instance."""
    global _skill_registry_instance
    if _skill_registry_instance is None:
        _skill_registry_instance = SkillRegistry()
        try:
            from core.registry import set_skill_registry_provider

            set_skill_registry_provider(get_skill_registry)
        except Exception:  # noqa: BLE001
            pass
    return _skill_registry_instance


# Global singleton registry instance
skill_registry: SkillRegistry = get_skill_registry()
try:
    from core.registry import set_skill_registry_provider

    set_skill_registry_provider(get_skill_registry)
except Exception:  # noqa: BLE001
    pass
