from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMExecutor(Protocol):
    """Executes an LLM call with retry, backoff, and supersession protection."""

    async def __call__(
        self,
        func: Any,
        kwargs: dict[str, Any],
        agent_key: str,
        thread_key: tuple[str, str],
        is_superseded_fn: Any = None,
        max_attempts: int = 3,
        run_in_threadpool_fn: Any = None,
        allow_503_immediate_retry: bool = True,
    ) -> Any: ...


@runtime_checkable
class SlackPoster(Protocol):
    """Posts an agent response to a Slack thread using dynamic persona customization."""

    async def __call__(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str,
        icon_url: str | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class HistoryFetcher(Protocol):
    """Fetches past thread messages formatted as LLM conversation turns."""

    async def __call__(
        self, channel_id: str, thread_ts: str, bot_user_id: str | None = None
    ) -> list[dict[str, str]]: ...


@runtime_checkable
class FilesFetcher(Protocol):
    """Fetches and parses file attachments from a Slack thread."""

    async def __call__(
        self, channel_id: str, thread_ts: str
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class TokenRecorder(Protocol):
    """Records consumed LLM tokens for a thread session."""

    def __call__(
        self,
        channel_id: str,
        thread_ts: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        agent_name: str | None = None,
    ) -> None: ...


@runtime_checkable
class NotesUpdater(Protocol):
    """Triggers background updates to thread notes summary."""

    def __call__(
        self, channel_id: str, thread_ts: str, is_completed: bool = False
    ) -> Any: ...


@runtime_checkable
class UsageUpdater(Protocol):
    """Triggers background updates to the Slack usage channel."""

    def __call__(
        self, channel_id: str, thread_ts: str, is_completed: bool = False
    ) -> Any: ...


@runtime_checkable
class SleepFunction(Protocol):
    """Asynchronous sleep callable."""

    async def __call__(self, seconds: float) -> None: ...


@runtime_checkable
class SystemMessagePoster(Protocol):
    """Posts a system message / alert to a Slack thread."""

    async def __call__(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class SlackMessageUpdater(Protocol):
    """Updates an existing Slack message in-place."""

    async def __call__(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class CostCalculator(Protocol):
    """Calculates total monetary and token expenditure for a thread."""

    def __call__(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, Any]: ...


__all__ = [
    "CostCalculator",
    "FilesFetcher",
    "HistoryFetcher",
    "LLMExecutor",
    "NotesUpdater",
    "SlackMessageUpdater",
    "SlackPoster",
    "SleepFunction",
    "SystemMessagePoster",
    "TokenRecorder",
    "UsageUpdater",
]
