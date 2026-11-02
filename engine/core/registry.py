from collections.abc import Callable
from typing import Any

_skill_registry_provider: Callable[[], Any] | None = None


def set_skill_registry_provider(provider: Callable[[], Any] | None) -> None:
    """Injectable provider for SkillRegistry to decouple core from skills layer."""
    global _skill_registry_provider
    _skill_registry_provider = provider


def get_skill_registry() -> Any:
    """
    Returns the global SkillRegistry instance.
    Uses registered provider if set, or falls back to skills.loader.
    """
    if _skill_registry_provider is not None:
        return _skill_registry_provider()
    from skills.loader import get_skill_registry as _get_reg

    return _get_reg()

