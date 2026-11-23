"""Tool authorization and RBAC policy helpers for skills and ToolPrincipal identities."""

from __future__ import annotations

import logging
from typing import Any

from core.domain import ToolPrincipal
from core.registry import get_skill_registry

logger = logging.getLogger(__name__)

# Sentinel constant to allow internal/system unrestricted tool execution
ALL_INTERNAL_SKILLS = "__ALL_INTERNAL_SKILLS__"


def normalize_skills_list(skills: list[str] | set[str] | str | None) -> list[str]:
    """Safely normalizes authorized skills, avoiding character slicing on strings."""
    if skills is None:
        return []
    if isinstance(skills, str):
        return [skills]
    return list(skills)


def _collect_skill_tools(s_def: Any) -> set[str]:
    """Collects static, custom, and cached MCP tool names declared by a SkillDefinition."""
    collected: set[str] = set(getattr(s_def, "tools", []))
    for schema in getattr(s_def, "custom_tool_schemas", []):
        fn = schema.get("function", {}).get("name")
        if fn:
            collected.add(fn)
    if getattr(s_def, "mcp_server", ""):
        try:
            from services.mcp.client_manager import get_mcp_client_manager

            mgr = get_mcp_client_manager()
            allowed = getattr(s_def, "mcp_tools", None)
            allowed_raw = allowed if (allowed and "*" not in allowed) else None
            for t in mgr.get_cached_tools_for_server(s_def.mcp_server, allowed_raw_names=allowed_raw):
                collected.add(t.namespaced_name)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as e:
            logger.debug("[SKILLS] Skipping cached MCP tools for skill '%s': %s", getattr(s_def, "name", "?"), e)
    return collected


def get_permitted_tools_for_principal(
    principal: Any,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> set[str]:
    """Returns the complete set of permitted tools for a given principal and skill set."""
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
            all_tools.update(_collect_skill_tools(s))
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
            from config_store.agent_settings import get_agent_skills

            raw_static = get_agent_skills(actor_id) or []
            static_skills = [
                s
                for s in raw_static
                if not getattr(
                    skill_registry.get(s) if hasattr(skill_registry, "get") else None,
                    "requires_admin",
                    False,
                )
            ]
        except (ImportError, AttributeError, KeyError, TypeError) as e:
            logger.debug("[SKILLS] Failed loading static skills for '%s': %s", actor_id, e)

    combined_skills = set(static_skills) | set(skills_list)
    permitted: set[str] = set()

    for s_name in combined_skills:
        s_def = skill_registry.get(s_name) if hasattr(skill_registry, "get") else None
        if s_def:
            permitted.update(_collect_skill_tools(s_def))

    skills_iter = (
        skill_registry.list_skills()
        if hasattr(skill_registry, "list_skills")
        else list(skill_registry._skills.values())
    )
    for s_def in skills_iter:
        if getattr(s_def, "is_global", False):
            if getattr(s_def, "requires_admin", False) and s_def.name not in combined_skills:
                continue
            permitted.update(_collect_skill_tools(s_def))

    return permitted


def is_principal_authorized_for_tool(
    principal: Any,
    tool_name: str,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> bool:
    """Validates whether a principal has authorization to execute a tool (fail-closed)."""
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

