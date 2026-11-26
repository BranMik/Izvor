from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapters.slack import PROCESSED_EVENTS, slack_router

__all__ = [
    "PROCESSED_EVENTS",
    "slack_router",
]


def __getattr__(name: str) -> Any:
    """Lazily re-exports Slack adapter symbols to keep workspace_adapter imports lightweight."""
    if name in ("PROCESSED_EVENTS", "slack_router"):
        from adapters import slack as _slack

        return getattr(_slack, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
