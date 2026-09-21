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
    is_terminal: bool = False
    extraction_rule: str = ""
    handles_files: bool = False

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
                    logger.debug(
                        f"[SKILLS] Loaded declarative skill '{skill_def.name}' ({skill_def.scope}) from {skill_file}"
                    )
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
        display_name = str(
            meta.get("display_name", name.replace("_", " ").title())
        ).strip()
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
                logger.error(
                    f"[SKILLS] Error loading action handler from {action_file}: {e}"
                )

        extraction_rule = str(meta.get("extraction_rule") or triggers.get("rule", "")).strip()
        if "{name}" in extraction_rule:
            extraction_rule = extraction_rule.replace("{name}", name)
        handles_files = bool(meta.get("handles_files") or triggers.get("handles_files", False))
        is_terminal = bool(meta.get("terminal") or triggers.get("terminal", False) or triggers.get("arg_type") == "terminal_flag")

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
            extraction_rule=extraction_rule,
            handles_files=handles_files,
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

    def get_agent_skills(
        self, allowed_skills: list[str] | None = None
    ) -> list[SkillDefinition]:
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
        import re

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
            if "read_document" in skill.tools or "read_file" in skill.tools:
                return skill
        return None

    def get_auto_detect_skills(self) -> list[SkillDefinition]:
        """Returns all skills where auto_detect is not False (default True for all skills)."""
        return [
            s
            for s in self._skills.values()
            if s.triggers.get("auto_detect", True) is not False
        ]

    def build_extractor_catalog_prompt(self) -> str:
        """
        Dynamically generates the system prompt for the IntentExtractor LLM
        based on all registered auto-detect skills and their extraction rules.
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

        special_rules = []
        for skill in skills:
            if skill.extraction_rule:
                special_rules.append(f"- '{skill.name}': {skill.extraction_rule}")

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
                "  ]",
                "}",
            ]
        )
        prompt_lines.extend(schema_lines)
        return "\n".join(prompt_lines)

    def get_tools_for_skills(
        self, skill_names: list[str] | None
    ) -> list[dict[str, Any]]:
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
            skills_to_show = [
                s for s in skills_to_show if s.name in allowed_skills or s.is_global
            ]
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
                "- If your turn is purely conversational, conceptual, or philosophical, do NOT load any skills—respond directly.",
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
        except Exception:
            agents_cfg = AGENTS_CONFIG

        thread_skills: list[str] = []
        thread_agent_skills: dict[str, list[str]] = {}
        if channel_id and thread_ts:
            try:
                from orchestration.thread_skills_manager import thread_skills_manager

                thread_skills = thread_skills_manager.get_active_skills(
                    channel_id, thread_ts
                )
                thread_agent_skills = thread_skills_manager.get_all_active_agent_skills(
                    channel_id, thread_ts
                )
            except Exception:
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
                "authorized_agents": auth_agents,
            }
        return mapping

    def format_skills_directory_markdown(
        self, channel_id: str | None = None, thread_ts: str | None = None
    ) -> str:
        """
        Formats the full platform skills directory into clean markdown.
        Beside each skill is listed all agents who can use it.
        """
        mapping = self.get_skill_agent_mapping(
            channel_id=channel_id, thread_ts=thread_ts
        )
        lines = ["*Application Skills & Capabilities Directory*:\n"]

        agent_skills = []
        directive_skills = []
        modifier_skills = []

        for name, info in sorted(mapping.items()):
            agents_str = ", ".join(info["authorized_agents"])
            desc = info["description"] or "No description provided."
            entry = f"• `{name}`: {desc}\n   ↳ *Authorized*: {agents_str}"
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
            "_Note: Agents are strictly limited to their assigned permissions and cannot execute unauthorized skills, unless /grant_for_all instructions was given in a thread._"
        )
        return "\n".join(lines)

    def get_skills_directory_prompt(self) -> str:
        """
        Generates the comprehensive application skills directory for injection into agent system prompts.
        Informs all agents of platform skills and which agents can use them, while enforcing boundary awareness.
        """
        mapping = self.get_skill_agent_mapping()
        lines = [
            "### APPLICATION SKILLS DIRECTORY",
            "The platform provides the following skills. Beside each skill is the list of agents authorized to use it:",
        ]
        for name, info in sorted(mapping.items()):
            agents_str = ", ".join(info["authorized_agents"])
            lines.append(
                f"- `{name}`: {info['description']} (Authorized: {agents_str})"
            )

        lines.extend(
            [
                "",
                "PERMISSION BOUNDARIES & USAGE RESTRICTIONS:",
                "- You can view and reference this directory to accurately answer user questions about platform capabilities and agent assignments.",
                "- However, you are strictly limited in skills usage. You may ONLY load and execute tools for skills specifically authorized for your role.",
                "- If a user requests an action requiring a skill you are not authorized to use, state clearly that you do not have permission and direct them to an authorized agent.",
                "- You must NEVER attempt to circumvent permission boundaries.",
            ]
        )
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

            if (
                authorized_skills is not None
                and skill_name not in authorized_skills
                and not is_globally_allowed
            ):
                if expired_skills and skill_name in expired_skills:
                    ttl_str = (
                        f"{ttl_hours:.1f} hours"
                        if ttl_hours
                        else "the expiration limit"
                    )
                    return json.dumps(
                        {
                            "error": (
                                f"Permission denied: Thread global skill '{skill_name}' expired after {ttl_str}. "
                                f"Run `/grant_for_all {skill_name}` in this thread to grant access again."
                            )
                        }
                    )
                allowed_str = ", ".join(f"'{s}'" for s in authorized_skills) or "none"
                return json.dumps(
                    {
                        "error": (
                            f"Permission denied: Agent is not authorized to load skill '{skill_name}'. "
                            f"Permitted skills: {allowed_str}. "
                            f"CRITICAL INSTRUCTION: You do NOT have permission to load or use the skill '{skill_name}'. "
                            f"You must explicitly state to the user that you do not have permission / the required skill '{skill_name}' to access or process this resource. "
                            f"Do NOT report that the file or resource was not found."
                        )
                    }
                )

            skill = target_skill
            if not skill:
                avail = ", ".join(f"'{s.name}'" for s in self.list_skills())
                return json.dumps(
                    {
                        "error": f"Skill '{skill_name}' not found. Available skills: {avail}"
                    }
                )

            return json.dumps(
                {
                    "status": "loaded",
                    "skill": skill.name,
                    "display_name": skill.display_name,
                    "unlocked_tools": skill.tools,
                    "operational_guidelines": skill.instructions,
                },
                indent=2,
            )

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
                allowed_tools_str = (
                    ", ".join(f"'{t}'" for t in sorted(permitted_tools)) or "none"
                )
                return json.dumps(
                    {
                        "error": (
                            f"Permission denied: Agent is not authorized to execute tool '{tool_name}'. "
                            f"Permitted tools: {allowed_tools_str}. "
                            f"CRITICAL INSTRUCTION: You do NOT have permission to execute tool '{tool_name}'. "
                            f"You must explicitly state to the user that you do not have permission to execute this tool. "
                            f"Do NOT report that the file or resource was not found."
                        )
                    }
                )

        return execute_mcp_tool(tool_name, arguments)


# Global singleton registry instance
skill_registry = SkillRegistry()
