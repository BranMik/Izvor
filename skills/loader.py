"""
Declarative SKILL.md Loader & Progressive Disclosure Registry.
Discovers and parses SKILL.md files (YAML frontmatter + Markdown body)
and binds low-level MCP execution backends.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from services.mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent

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
                        "Name of the skill to load (e.g. 'web_research', "
                        "'codebase_inspection', 'workspace_authoring')."
                    ),
                }
            },
            "required": ["skill_name"],
        },
    },
}


import importlib.util
from collections.abc import Callable

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

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Returns the LiteLLM tool schemas for the tools declared by this skill."""
        matched: list[dict[str, Any]] = []
        for schema in MCP_TOOLS_SCHEMAS:
            fn_name = schema.get("function", {}).get("name")
            if fn_name in self.tools:
                matched.append(schema)
        return matched


class SkillRegistry:
    """
    Registry that dynamically scans and loads all SKILL.md files.
    """

    def __init__(self, root_dir: Path = SKILLS_DIR) -> None:
        self.root_dir = root_dir
        self._skills: dict[str, SkillDefinition] = {}
        self.reload()

    def reload(self) -> None:
        """Scans root_dir for all subdirectories containing SKILL.md."""
        self._skills.clear()
        if not self.root_dir.exists():
            return

        for skill_dir in self.root_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                skill_def = self._parse_skill_file(skill_file)
                if skill_def:
                    self._skills[skill_def.name] = skill_def
                    logger.debug(f"[SKILLS] Loaded declarative skill '{skill_def.name}' ({skill_def.scope}) from {skill_file}")
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.error(f"[SKILLS] File I/O error reading {skill_file}: {e}")
            except (yaml.YAMLError, ValueError, TypeError, UnicodeError) as e:
                logger.error(f"[SKILLS] Parse error in {skill_file}: {e}")
            except Exception as e:
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

        meta = yaml.safe_load(frontmatter_str) or {}
        name = str(meta.get("name", file_path.parent.name)).strip()
        display_name = str(meta.get("display_name", name.replace("_", " ").title())).strip()
        description = str(meta.get("description", "")).strip()
        scope = str(meta.get("scope", "agent")).strip().lower()
        triggers = meta.get("triggers") or {}
        if not isinstance(triggers, dict):
            triggers = {}

        raw_tools = meta.get("tools", [])
        tools = [str(t).strip() for t in raw_tools if str(t).strip()]

        is_global = bool(meta.get("global", meta.get("all_agents", False)))

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
                        action_handler = getattr(mod, "run")
            except Exception as e:
                logger.error(f"[SKILLS] Error loading action handler from {action_file}: {e}")

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
        )

    def get(self, name: str) -> SkillDefinition | None:
        """Retrieve a skill by name."""
        return self._skills.get(name)

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

    def get_auto_detect_skills(self) -> list[SkillDefinition]:
        """Returns all skills where auto_detect is not False (default True for all skills)."""
        return [
            s for s in self._skills.values()
            if s.triggers.get("auto_detect", True) is not False
        ]

    def build_extractor_catalog_prompt(self) -> str:
        """
        Dynamically generates the system prompt for the IntentExtractor LLM
        based on all registered auto-detect skills.
        """
        skills = self.get_auto_detect_skills()
        prompt_lines = [
            "You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.",
            "Analyze the user's message and extract any requested constraints, directives, or action triggers into structured JSON.",
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
                arg_type = skill.triggers.get("arg_type", "object")
                actions_lines.append(f'- "{skill.name}": {desc}')

        if modifiers_lines:
            prompt_lines.append("MODIFIERS (Output constraints & styling):")
            prompt_lines.extend(modifiers_lines)
            prompt_lines.append("")

        if actions_lines:
            prompt_lines.append("ACTIONS (Operations, tools & pipeline triggers):")
            prompt_lines.extend(actions_lines)
            prompt_lines.append("")

        prompt_lines.extend([
            "SPECIAL RULES:",
            "- 'max_words': Extract integer ceiling only if user requested a maximum length (e.g. 'in under 50 words', 'max 30 words', 'briefly in 40 words'). Do NOT extract minimum length as max_words.",
            "- 'min_words': Extract integer floor only if user requested a minimum length (e.g. 'at least 100 words', 'minimalno 50 riječi').",
            "- 'history': Extract integer message limit if requested (e.g. 'last 3 messages', 'zadnje 2 poruke').",
            "- 'document_reader': If user asks to read, analyze, inspect, or reference an attached file, include action 'document_reader' with param 'target_file' (or null if generic).",
            "- 'exclude_agents': If user asks to exclude or leave out specific agents, include action 'exclude_agents' with param 'agents' (list of string agent keys). Agents tagged to speak must NEVER be excluded.",
            "",
            "Respond ONLY with a valid JSON object matching this schema:",
            "{",
            '  "modifiers": {',
            '    "max_words": <int or null>,',
            '    "min_words": <int or null>,',
            '    "history": <int or null>',
            "  },",
            '  "actions": [',
            '    { "name": "<skill_name>", "params": { ... } }',
            "  ]",
            "}",
        ])
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

        lines.extend([
            "",
            "SKILL INFERENCE RULE:",
            "- If your turn is purely conversational, conceptual, or philosophical, do NOT load any skills—respond directly.",
            "- If you need external facts or file access, call `load_skill` with the relevant skill name.",
        ])
        return "\n".join(lines) + "\n"

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        authorized_skills: list[str] | None = None,
        expired_skills: list[str] | None = None,
        ttl_hours: float | None = None,
    ) -> str:
        """
        Dispatches tool execution.
        If tool is 'load_skill', enforces authorization against authorized_skills and
        returns the skill's SKILL.md guidelines.
        Otherwise delegates to services.mcp_tools.execute_mcp_tool.
        """
        if tool_name == "load_skill":
            skill_name = arguments.get("skill_name", "").strip()
            target_skill = self.get(skill_name)
            is_globally_allowed = bool(target_skill and target_skill.is_global)

            if authorized_skills is not None and skill_name not in authorized_skills and not is_globally_allowed:
                if expired_skills and skill_name in expired_skills:
                    ttl_str = f"{ttl_hours:.1f} hours" if ttl_hours else "the expiration limit"
                    return json.dumps({
                        "error": (
                            f"Permission denied: Thread global skill '{skill_name}' expired after {ttl_str}. "
                            f"Run `/grant_for_all {skill_name}` in this thread to grant access again."
                        )
                    })
                allowed_str = ", ".join(f"'{s}'" for s in authorized_skills) or "none"
                return json.dumps({
                    "error": (
                        f"Permission denied: Agent is not authorized to load skill '{skill_name}'. "
                        f"Permitted skills: {allowed_str}"
                    )
                })

            skill = target_skill
            if not skill:
                avail = ", ".join(f"'{s.name}'" for s in self.list_skills())
                return json.dumps({
                    "error": f"Skill '{skill_name}' not found. Available skills: {avail}"
                })

            return json.dumps({
                "status": "loaded",
                "skill": skill.name,
                "display_name": skill.display_name,
                "unlocked_tools": skill.tools,
                "operational_guidelines": skill.instructions,
            }, indent=2)

        # For any other tool, verify authorization against authorized_skills if provided
        if authorized_skills is not None:
            permitted_tools: set[str] = set()
            for s_name in authorized_skills:
                s_def = self.get(s_name)
                if s_def:
                    permitted_tools.update(s_def.tools)
            for s_def in self._skills.values():
                if s_def.is_global:
                    permitted_tools.update(s_def.tools)
            if tool_name not in permitted_tools:
                allowed_tools_str = ", ".join(f"'{t}'" for t in sorted(permitted_tools)) or "none"
                return json.dumps({
                    "error": (
                        f"Permission denied: Agent is not authorized to execute tool '{tool_name}'. "
                        f"Permitted tools: {allowed_tools_str}"
                    )
                })

        return execute_mcp_tool(tool_name, arguments)


# Global singleton registry instance
skill_registry = SkillRegistry()
