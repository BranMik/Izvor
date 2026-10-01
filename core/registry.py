from typing import Any

# Global singleton instance for the SkillRegistry
_skill_registry: Any = None

def get_skill_registry() -> Any:
    """
    Returns the global SkillRegistry instance.
    Handles the lazy import of the skills module centrally to prevent
    circular dependencies across the codebase.
    """
    global _skill_registry
    if _skill_registry is None:
        from skills import skill_registry
        _skill_registry = skill_registry
    return _skill_registry
