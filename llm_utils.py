import logging

logger = logging.getLogger(__name__)


def is_anthropic_model(model: str | None) -> bool:
    """Checks whether the given model name targets Anthropic (Claude)."""
    if not model or not isinstance(model, str):
        return False
    m = model.lower().strip()
    return m.startswith("anthropic/") or "claude" in m


def is_openai_model(model: str | None) -> bool:
    """Checks whether the given model name targets OpenAI."""
    if not model or not isinstance(model, str):
        return False
    m = model.lower().strip()
    return (
        m.startswith("openai/")
        or "gpt" in m
        or "chatgpt" in m
        or m.startswith("o1")
        or m.startswith("o3")
        or "/o1" in m
        or "/o3" in m
    )
