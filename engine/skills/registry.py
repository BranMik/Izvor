"""Declarative SkillRegistry with lazy thread-safe initialization (zero import-time disk scans)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any

import yaml

from core.domain import ToolPrincipal
from core.paths import ENGINE_SKILLS_DIR, SKILLS_DIR
from core.registry import set_skill_registry_provider
from services.local_tools import execute_local_tool
from skills.parser import (
    LOAD_SKILL_SCHEMA,  # noqa: F401 - re-exported for callers
    SkillDefinition,
    SkillIntegrityError,
    parse_skill_file,
)
from skills.policy import (
    ALL_INTERNAL_SKILLS,
    get_permitted_tools_for_principal,
    normalize_skills_list,
)
from skills.recommender import (
    get_skill_resolution_hint as _get_skill_resolution_hint,
    recommend_agent_for_skills as _recommend_agent_for_skills,
    select_recommended_agent_from_keys,
)

logger = logging.getLogger(__name__)


class SkillRegistry:
    """
    Registry that scans SKILL.md files from engine/skills/builtin/ and config/skills/.
    Supports explicit reload() at server startup or lazy ensure_loaded() on first query.
    """

    def __init__(
        self,
        root_dir: Path | list[Path] | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        if root_dir is None:
            self.root_dirs: list[Path] = [ENGINE_SKILLS_DIR, SKILLS_DIR]
            self.root_dir = SKILLS_DIR
        elif isinstance(root_dir, (list, tuple)):
            self.root_dirs = [Path(p) for p in root_dir]
            self.root_dir = self.root_dirs[-1]
        else:
            self.root_dirs = [Path(root_dir)]
            self.root_dir = Path(root_dir)
        self._lock = threading.RLock()
        self._skills: dict[str, SkillDefinition] = {}
        self._loaded = False
        if auto_load:
            self.reload()

    def ensure_loaded(self) -> None:
        """Lazily populates the registry once using double-checked locking."""
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self.reload()

    def reload(self) -> None:
        """Scans root_dirs for subdirectories containing SKILL.md and atomically swaps the registry."""
        new_skills: dict[str, SkillDefinition] = {}
        builtin_names: set[str] = set()
        try:
            resolved_builtin = ENGINE_SKILLS_DIR.resolve()
        except OSError:
            resolved_builtin = ENGINE_SKILLS_DIR

        for idx, r_dir in enumerate(self.root_dirs):
            if not r_dir.exists():
                continue
            try:
                is_builtin_dir = (r_dir.resolve() == resolved_builtin) or (idx == 0 and len(self.root_dirs) > 1)
            except OSError:
                is_builtin_dir = idx == 0 and len(self.root_dirs) > 1

            for skill_dir in sorted(r_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue

                try:
                    skill_def = self._parse_skill_file(skill_file)
                    if skill_def:
                        if not is_builtin_dir and skill_def.name in builtin_names:
                            logger.critical(
                                f"[SECURITY] Blocked attempt by '{skill_file}' to shadow builtin skill '{skill_def.name}'!"
                            )
                            try:
                                from services.audit_logger import log_audit_event

                                log_audit_event(
                                    event_type="SKILL_SHADOWING_BLOCKED",
                                    actor={"type": "system", "id": "skill_registry"},
                                    action="reload_skills",
                                    target=f"skill:{skill_def.name}",
                                    details={"shadow_path": str(skill_file)},
                                    severity="CRITICAL",
                                    status="BLOCKED",
                                )
                            except (ImportError, OSError, RuntimeError, ValueError, TypeError) as audit_err:
                                logger.debug(f"[SKILLS] Failed to emit SKILL_SHADOWING_BLOCKED audit event: {audit_err}")
                            continue

                        new_skills[skill_def.name] = skill_def
                        if is_builtin_dir:
                            builtin_names.add(skill_def.name)
                        logger.debug(
                            f"[SKILLS] Loaded declarative skill '{skill_def.name}' ({skill_def.scope}) from {skill_file}"
                        )
                except SkillIntegrityError:
                    raise
                except (OSError, yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError) as e:
                    logger.error(f"[SKILLS] Failed to parse {skill_file}: {e}")

        with self._lock:
            self._skills = new_skills
            self._loaded = True

    def _parse_skill_file(self, file_path: Path) -> SkillDefinition | None:
        return parse_skill_file(file_path)

    def get(self, name: str) -> SkillDefinition | None:
        """Retrieve a skill by name."""
        self.ensure_loaded()
        return self._skills.get(name)

    def resolve_skill(self, name_or_alias: str) -> SkillDefinition | None:
        """Resolves a skill by its canonical name or slash command alias (e.g. 'grant_for_all' or '/grant_for_all')."""
        if not name_or_alias:
            return None
        self.ensure_loaded()
        skills_snapshot = self._skills
        target = str(name_or_alias).strip().lower()
        if target in skills_snapshot:
            return skills_snapshot[target]
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
        self.ensure_loaded()
        skills_snapshot = self._skills
        return [s for s in skills_snapshot.values() if getattr(s, "requires_admin", False)]

    def filter_skills_for_caller(
        self,
        skill_names: list[str] | set[str] | None,
        is_admin: bool,
        thread_granted_skills: set[str] | list[str] | None = None,
    ) -> list[str]:
        """Filters skills requiring admin privileges unless caller is admin or explicitly thread-granted."""
        self.ensure_loaded()
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
        self.ensure_loaded()
        skills_snapshot = self._skills
        return list(skills_snapshot.values())

    def get_global_skills(self) -> list[str]:
        """Returns a list of names for skills marked global in SKILL.md."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        return [s.name for s in skills_snapshot.values() if s.is_global]

    def get_agent_skills(self, allowed_skills: list[str] | None = None) -> list[SkillDefinition]:
        """Returns skills with scope == 'agent', optionally filtered by allowed_skills and non-admin globals."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        res = [s for s in skills_snapshot.values() if s.scope == "agent"]
        if allowed_skills is not None:
            res = [
                s
                for s in res
                if s.name in allowed_skills or (s.is_global and not getattr(s, "requires_admin", False))
            ]
        return res

    def get_slash_triggers(self) -> dict[str, tuple[SkillDefinition, dict[str, Any]]]:
        """Maps each registered slash alias (lowercase) to its (SkillDefinition, trigger_dict)."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        mapping: dict[str, tuple[SkillDefinition, dict[str, Any]]] = {}
        for skill in skills_snapshot.values():
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
        self.ensure_loaded()
        skills_snapshot = self._skills
        terminals: list[str] = []
        for skill in skills_snapshot.values():
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
        self.ensure_loaded()
        skills_snapshot = self._skills
        for skill in skills_snapshot.values():
            if skill.handles_files:
                return skill
        for skill in skills_snapshot.values():
            if "read_document" in skill.tools or "write_document" in skill.tools:
                return skill
        return None

    def get_auto_detect_skills(self) -> list[SkillDefinition]:
        """Returns all skills where auto_detect is True (defaults to False if omitted)."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        return [s for s in skills_snapshot.values() if s.triggers.get("auto_detect", False) is True]

    def build_extractor_catalog_prompt(self) -> str:
        """Dynamically generates the IntentExtractor system prompt from registered auto-detect skills."""
        skills = self.get_auto_detect_skills()
        prompt_lines = [
            "You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.",
            "Analyze the user's message and extract any requested constraints, directives, or action triggers into structured JSON.",
            'Also detect the natural language of the user\'s message (e.g. "Croatian", "English", "German") and report it in the "language" field.',
            "",
            "AVAILABLE SYSTEM SKILLS & DIRECTIVES:",
        ]

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

        self.ensure_loaded()
        skills_snapshot = self._skills
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()

        for s_name in skill_names:
            skill = skills_snapshot.get(s_name)
            if not skill:
                continue
            for schema in skill.get_tool_schemas():
                fn_name = schema.get("function", {}).get("name")
                if fn_name and fn_name not in seen:
                    tools.append(schema)
                    seen.add(fn_name)

        return tools

    def find_skill_for_tool(self, tool_name: str) -> str | None:
        """Finds the skill name that defines or provides the given tool_name (including wildcard MCP skills)."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        for s_name, s_def in skills_snapshot.items():
            if tool_name in s_def.tools:
                return s_name
            for schema in s_def.custom_tool_schemas:
                if schema.get("function", {}).get("name") == tool_name:
                    return s_name

        if tool_name.startswith("mcp__"):
            from services.mcp.schema_translator import parse_mcp_tool_name

            parsed = parse_mcp_tool_name(tool_name)
            if parsed is not None:
                server_name, raw_tool_name = parsed
                for s_name, s_def in skills_snapshot.items():
                    if s_def.mcp_server == server_name:
                        mcp_tools = getattr(s_def, "mcp_tools", []) or []
                        if "*" in mcp_tools or raw_tool_name in mcp_tools:
                            return s_name
        return None

    def get_skills_catalog_prompt(self, allowed_skills: list[str] | None = None) -> str:
        """Generates the compact skills catalog for injection into an agent system prompt."""
        self.ensure_loaded()
        skills_snapshot = self._skills
        if not skills_snapshot:
            return ""

        skills_to_show = [s for s in skills_snapshot.values() if s.scope == "agent"]
        if allowed_skills is not None:
            skills_to_show = [
                s
                for s in skills_to_show
                if s.name in allowed_skills or (s.is_global and not getattr(s, "requires_admin", False))
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
                "- If your turn is purely conversational do NOT load any skills—respond directly.",
                "- If you need external facts or file access, call `load_skill` with the relevant skill name.",
            ]
        )
        return "\n".join(lines) + "\n"

    def get_skill_agent_mapping(
        self, channel_id: str | None = None, thread_ts: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Returns a dictionary mapping each registered skill name to its metadata and authorized agents."""
        from core.config import AGENTS_CONFIG

        try:
            from config_store.agent_settings import settings_manager

            agents_cfg = settings_manager.agents_config or AGENTS_CONFIG
        except (ImportError, AttributeError, TypeError):
            agents_cfg = AGENTS_CONFIG

        thread_skills: list[str] = []
        thread_agent_skills: dict[str, list[str]] = {}
        if channel_id and thread_ts:
            try:
                from services.thread_skills_store import thread_skills_manager

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
        """Formats the full platform skills directory into clean Slack markdown."""
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

    def recommend_agent_from_keys(
        self,
        candidate_keys: list[str] | set[str] | tuple[str, ...],
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        exclude_agent_key: str | None = None,
    ) -> str | None:
        """Selects 1 recommended agent username from a set of authorized agent keys."""
        return select_recommended_agent_from_keys(
            candidate_keys,
            channel_id=channel_id,
            thread_ts=thread_ts,
            exclude_agent_key=exclude_agent_key,
        )

    def recommend_agent_for_skills(
        self,
        skill_names: list[str] | str,
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        exclude_agent_key: str | None = None,
    ) -> str | None:
        """Selects 1 recommended agent username authorized for the requested skill(s)."""
        return _recommend_agent_for_skills(
            skill_names,
            channel_id=channel_id,
            thread_ts=thread_ts,
            exclude_agent_key=exclude_agent_key,
        )

    def get_skill_resolution_hint(
        self,
        skill_names: list[str] | str,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        exclude_agent_key: str | None = None,
    ) -> str:
        """Generates a concise 1-line resolution hint recommending an agent or /grant_skill."""
        return _get_skill_resolution_hint(
            skill_names,
            channel_id=channel_id,
            thread_ts=thread_ts,
            exclude_agent_key=exclude_agent_key,
        )

    def get_skills_directory_prompt(
        self, channel_id: str | None = None, thread_ts: str | None = None
    ) -> str:
        """Generates the comprehensive application skills directory for injection into agent system prompts."""
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
                "- When a user requests an action requiring an unauthorized skill that is not authorized for you in this thread, explicitly report lack of permission in at most 50 words (including a 1-line hint recommending just 1 authorized agent by name—never by agent ID—or /grant_skill) without reflecting on skills architecture, and proceed normally with any other parts of the prompt that do not require the missing skill.",
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
        """Unified tool and skill authorization check with audit event logging."""
        self.ensure_loaded()
        is_system_principal = isinstance(agent_name, ToolPrincipal) and agent_name.kind == "system"
        is_internal = (
            is_system_principal
            or authorized_skills == ALL_INTERNAL_SKILLS
            or (isinstance(authorized_skills, (list, tuple, set)) and ALL_INTERNAL_SKILLS in authorized_skills)
        )
        actor_id = agent_name.id if hasattr(agent_name, "id") else str(agent_name or "unknown_agent")
        actor_kind = getattr(agent_name, "kind", "agent")

        skills_snapshot = self._skills
        normalized_auth = normalize_skills_list(authorized_skills)
        effective_authorized_skills: list[str] = (
            [s.name for s in skills_snapshot.values()] if is_internal else normalized_auth
        )

        if tool_name == "load_skill":
            skill_name = arguments.get("skill_name", "").strip()
            target_skill = skills_snapshot.get(skill_name)
            is_globally_allowed = bool(
                target_skill and target_skill.is_global and not getattr(target_skill, "requires_admin", False)
            )

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
                hint_str = self.get_skill_resolution_hint(skill_name, exclude_agent_key=actor_id)
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
                                f"including this 1-line hint (recommend ONLY this 1 agent by name, never by agent ID): {hint_str} "
                                "Do NOT report that the file or resource was not found. Do NOT reflect on skills architecture. "
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
                except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as e:
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

        if not is_internal:
            permitted_tools = get_permitted_tools_for_principal(
                principal=agent_name,
                authorized_skills=effective_authorized_skills,
                skill_registry=self,
            )

            if tool_name not in permitted_tools:
                allowed_tools_str = ", ".join(f"'{t}'" for t in sorted(permitted_tools)) or "none"
                s_name = self.find_skill_for_tool(tool_name) or tool_name
                hint_str = self.get_skill_resolution_hint(s_name, exclude_agent_key=actor_id)
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
                                f"including this 1-line hint (recommend ONLY this 1 agent by name, never by agent ID): {hint_str} "
                                "Do NOT report that the file or resource was not found. Do NOT reflect on skills architecture. "
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
        token_mapping: dict[str, str] | None = None,
    ) -> str:
        """Asynchronously dispatches tool execution with fail-closed authorization."""
        self.ensure_loaded()
        is_ok, early_resp, effective_authorized_skills = self._verify_tool_authorization(
            tool_name=tool_name,
            arguments=arguments,
            authorized_skills=authorized_skills,
            expired_skills=expired_skills,
            ttl_hours=ttl_hours,
            agent_name=agent_name,
        )
        if not is_ok or early_resp is not None:
            return early_resp or ""

        from services.mcp.schema_translator import is_mcp_namespaced_tool

        if is_mcp_namespaced_tool(tool_name):
            from services.mcp.security_bridge import execute_external_mcp_tool

            return await execute_external_mcp_tool(
                namespaced_tool_name=tool_name,
                arguments=arguments,
                agent_name=agent_name,
                authorized_skills=effective_authorized_skills,
                skill_registry=self,
                token_mapping=token_mapping,
            )

        skills_snapshot = self._skills
        for s_def in skills_snapshot.values():
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
                    kwargs: dict[str, Any] = {}
                    if "agent_name" in sig.parameters:
                        kwargs["agent_name"] = actor_id
                    if "token_mapping" in sig.parameters:
                        kwargs["token_mapping"] = token_mapping
                    if inspect.iscoroutinefunction(async_fn):
                        return await async_fn(arguments, **kwargs)
                    return await asyncio.to_thread(handler, arguments, **kwargs)
                except Exception as e:  # noqa: BLE001 - Isolate arbitrary skill plugin errors
                    logger.error(f"[SKILLS] Custom tool handler error for '{tool_name}': {e}")
                    return json.dumps({"error": f"Tool execution failed: {e}"})

        return await execute_local_tool(
            tool_name,
            arguments,
            agent_name=agent_name,
            authorized_skills=list(effective_authorized_skills),
        )


_skill_registry_instance: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """Returns the canonical global SkillRegistry singleton instance (lazy, zero import-time I/O)."""
    global _skill_registry_instance
    if _skill_registry_instance is None:
        _skill_registry_instance = SkillRegistry(auto_load=False)
        set_skill_registry_provider(get_skill_registry)
    return _skill_registry_instance


# Global singleton registry instance (explicitly loaded in lifespan or lazily via ensure_loaded)
skill_registry: SkillRegistry = get_skill_registry()

