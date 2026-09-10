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


@dataclass
class SkillDefinition:
    """Represents a declarative Agent Skill parsed from a SKILL.md file."""

    name: str
    display_name: str
    description: str
    tools: list[str] = field(default_factory=list)
    instructions: str = ""
    skill_dir: Path | None = None

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
                    logger.debug(f"[SKILLS] Loaded declarative skill '{skill_def.name}' from {skill_file}")
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
        raw_tools = meta.get("tools", [])
        tools = [str(t).strip() for t in raw_tools if str(t).strip()]

        return SkillDefinition(
            name=name,
            display_name=display_name,
            description=description,
            tools=tools,
            instructions=body_markdown,
            skill_dir=file_path.parent,
        )

    def get(self, name: str) -> SkillDefinition | None:
        """Retrieve a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[SkillDefinition]:
        """Returns all registered skills."""
        return list(self._skills.values())

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

        skills_to_show = list(self._skills.values())
        if allowed_skills is not None:
            if not allowed_skills:
                return ""
            skills_to_show = [s for s in self._skills.values() if s.name in allowed_skills]
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
    ) -> str:
        """
        Dispatches tool execution.
        If tool is 'load_skill', enforces authorization against authorized_skills and
        returns the skill's SKILL.md guidelines.
        Otherwise delegates to services.mcp_tools.execute_mcp_tool.
        """
        if tool_name == "load_skill":
            skill_name = arguments.get("skill_name", "").strip()
            if authorized_skills is not None and skill_name not in authorized_skills:
                allowed_str = ", ".join(f"'{s}'" for s in authorized_skills) or "none"
                return json.dumps({
                    "error": (
                        f"Permission denied: Agent is not authorized to load skill '{skill_name}'. "
                        f"Permitted skills: {allowed_str}"
                    )
                })

            skill = self.get(skill_name)
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
