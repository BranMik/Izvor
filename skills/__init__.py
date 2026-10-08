"""
Declarative Agent Skills Framework for Izvor.
Discovers SKILL.md documents and enables LLM-driven progressive disclosure.
"""

from .loader import (
    LOAD_SKILL_SCHEMA,
    SkillDefinition,
    SkillRegistry,
    skill_registry,
)

__all__ = [
    "LOAD_SKILL_SCHEMA",
    "SkillDefinition",
    "SkillRegistry",
    "skill_registry",
]
