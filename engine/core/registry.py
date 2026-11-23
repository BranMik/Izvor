from __future__ import annotations

from collections.abc import Callable
from typing import Any

_skill_registry_provider: Callable[[], Any] | None = None
_workspace_adapter_factory: Callable[..., Any] | None = None
_token_recorder_hook: Callable[..., Any] | None = None
_initial_agents_provider: Callable[[str | None, str | None], list[str]] | None = None
_thread_state_provider: Callable[[], Any] | None = None


def set_skill_registry_provider(provider: Callable[[], Any] | None) -> None:
    """Injectable provider for SkillRegistry to decouple core from skills layer."""
    global _skill_registry_provider
    _skill_registry_provider = provider


def get_skill_registry() -> Any:
    """
    Returns the global SkillRegistry instance.
    Uses registered provider if set, or raises RuntimeError if not configured.
    """
    if _skill_registry_provider is not None:
        return _skill_registry_provider()
    raise RuntimeError("Skill registry provider not configured. Call set_skill_registry_provider().")


def set_default_workspace_adapter_factory(factory: Callable[..., Any] | None) -> None:
    """Injectable factory for default WorkspaceAdapter (decouples L3 orchestration from L4 adapters)."""
    global _workspace_adapter_factory
    _workspace_adapter_factory = factory


def get_default_workspace_adapter_factory() -> Callable[..., Any] | None:
    """Returns the registered WorkspaceAdapter factory if configured."""
    return _workspace_adapter_factory


def register_token_recorder(recorder: Callable[..., Any] | None) -> None:
    """Registers token usage recorder callback (decouples L2 nlp/skills from L3 agents)."""
    global _token_recorder_hook
    _token_recorder_hook = recorder


def record_token_usage(
    *,
    channel_id: str,
    thread_ts: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    channel_name: str | None = None,
) -> dict[str, Any]:
    """Records token usage via registered recorder hook, or returns empty dict if unregistered."""
    if _token_recorder_hook is None:
        return {}
    return _token_recorder_hook(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        channel_name=channel_name,
    )


def set_initial_agents_provider(
    provider: Callable[[str | None, str | None], list[str]] | None,
) -> None:
    """Registers initial agents provider (decouples L2 skills from L3 orchestration.nomination)."""
    global _initial_agents_provider
    _initial_agents_provider = provider


def get_initial_agents_from_provider(
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> list[str]:
    """Queries registered initial agents provider, returning [] if unregistered."""
    if _initial_agents_provider is None:
        return []
    return list(_initial_agents_provider(channel_id, thread_ts) or [])


def set_thread_state_provider(provider: Callable[[], Any] | None) -> None:
    """Registers thread state manager provider (decouples L2 builtin skills from L3 thread_state)."""
    global _thread_state_provider
    _thread_state_provider = provider


def get_thread_state_manager() -> Any:
    """Returns registered thread_state_manager instance or raises RuntimeError."""
    if _thread_state_provider is None:
        raise RuntimeError("Thread state provider not configured. Call set_thread_state_provider().")
    return _thread_state_provider()
