from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapters.slack.events import PROCESSED_EVENTS, handle_slack_events
    from adapters.slack.interactivity import handle_slack_interactivity
    from adapters.slack.router import slack_router

__all__ = [
    "PROCESSED_EVENTS",
    "handle_slack_events",
    "handle_slack_interactivity",
    "slack_router",
]


def __getattr__(name: str) -> Any:
    """Lazily imports heavy Slack adapter routers and handlers on demand."""
    if name in ("PROCESSED_EVENTS", "handle_slack_events"):
        from adapters.slack import events as _events

        return getattr(_events, name)
    if name == "handle_slack_interactivity":
        from adapters.slack.interactivity import handle_slack_interactivity

        return handle_slack_interactivity
    if name == "slack_router":
        from adapters.slack.router import slack_router

        return slack_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
