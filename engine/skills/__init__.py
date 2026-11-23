"""Declarative agent skills framework (lazy package exports with zero import-time disk scans)."""

from __future__ import annotations

from skills.parser import LOAD_SKILL_SCHEMA, SkillDefinition, SkillIntegrityError
from skills.policy import (
    ALL_INTERNAL_SKILLS,
    get_permitted_tools_for_principal,
    is_principal_authorized_for_tool,
    normalize_skills_list,
)
from skills.recommender import (
    get_skill_resolution_hint,
    recommend_agent_for_skills,
    select_recommended_agent_from_keys,
)
from skills.registry import SkillRegistry, get_skill_registry, skill_registry

__all__ = [
    "ALL_INTERNAL_SKILLS",
    "LOAD_SKILL_SCHEMA",
    "SkillDefinition",
    "SkillIntegrityError",
    "SkillRegistry",
    "get_permitted_tools_for_principal",
    "get_skill_registry",
    "get_skill_resolution_hint",
    "is_principal_authorized_for_tool",
    "normalize_skills_list",
    "recommend_agent_for_skills",
    "select_recommended_agent_from_keys",
    "skill_registry",
]
