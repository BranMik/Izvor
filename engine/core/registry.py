from typing import Any


def get_skill_registry() -> Any:
    """
    Returns the global SkillRegistry instance.
    Centrally delegates to skills.loader.get_skill_registry() to prevent
    circular dependencies or duplicate instances across the codebase.
    """
    from skills.loader import get_skill_registry as _get_reg

    return _get_reg()

