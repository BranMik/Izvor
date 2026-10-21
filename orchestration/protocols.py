from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable



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


@runtime_checkable
class ToolExecutor(Protocol):
    """Low-level executor for a single tool call."""

    def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        agent_name: Any = None,
        authorized_skills: Any = None,
    ) -> Any: ...


@runtime_checkable
class WorkspaceAdapter(Protocol):
    """Hexagonal port decoupling core orchestration stages from chat-platform primitives."""

    def format_outbound_text(self, markdown_text: str) -> str: ...

    def build_budget_halt_blocks(
        self, message_text: str, session_id: str
    ) -> list[dict[str, Any]]: ...

    async def post_agent_turn(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str,
        icon_url: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    async def update_agent_turn(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    async def post_system_notice(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class FlatHistoryRecorder(Protocol):
    """Records thread transcript to flat markdown file."""

    async def __call__(
        self, channel_id: str, thread_ts: str, is_completed: bool = False
    ) -> Any: ...


@dataclass(frozen=True)
class PipelineDeps:
    """Explicit dependency injection container for TurnPipeline and stages."""

    llm_executor: LLMExecutor | None = None
    tool_executor: ToolExecutor | None = None
    workspace_adapter: WorkspaceAdapter | None = None
    history_fetcher: HistoryFetcher | None = None
    files_fetcher: FilesFetcher | None = None
    token_recorder: TokenRecorder | None = None
    notes_updater: NotesUpdater | None = None
    usage_updater: UsageUpdater | None = None
    calc_costs_fn: CostCalculator | None = None
    flat_history_recorder: FlatHistoryRecorder | None = None
    sleep_fn: SleepFunction | None = None
    post_system_message_fn: SystemMessagePoster | None = None
    update_message_fn: SlackMessageUpdater | None = None
    slack_poster: SlackPoster | None = None
    run_in_threadpool_fn: Any = None
    download_file_fn: Any = None
    debate_turn_pacing_seconds: float | None = None
    slack_post_pause_seconds: float | None = None
    max_tool_iterations: int | None = None
    timeout_secs: float | None = None
    intermediate_delay_secs: float | None = None


def resolve_pipeline_deps(
    deps: PipelineDeps | None = None,
    hooks: dict[str, Any] | None = None,
    **overrides: Any,
) -> PipelineDeps:
    """
    Constructs a fully populated, immutable PipelineDeps instance.
    Injected arguments take highest precedence, followed by hooks, followed by production defaults.
    """
    hooks = hooks or {}

    # Helper to resolve field value: overrides -> deps -> hooks -> lazy production default
    def _get(field_name: str, alt_hook_names: list[str] | None = None, default_factory: Callable[[], Any] | None = None) -> Any:
        if field_name in overrides and overrides[field_name] is not None:
            return overrides[field_name]
        if deps is not None and getattr(deps, field_name, None) is not None:
            return getattr(deps, field_name)
        if field_name in hooks and hooks[field_name] is not None:
            return hooks[field_name]
        if alt_hook_names:
            for alt in alt_hook_names:
                if alt in hooks and hooks[alt] is not None:
                    return hooks[alt]
        if default_factory is not None:
            return default_factory()
        return None

    from core.llm_utils import execute_llm_with_retry
    from services.mcp_tools import execute_mcp_tool
    from services.slack_client import (
        download_slack_file,
        fetch_thread_files,
        fetch_thread_history,
        post_agent_response,
        post_system_message,
        update_slack_message,
    )
    from services.flat_history_recorder import async_record_flat_history
    from agents.thread_notes_agent import trigger_thread_notes_update
    from agents.usage_agent import (
        calculate_thread_costs,
        record_thread_tokens,
        trigger_usage_update,
    )
    import asyncio
    from starlette.concurrency import run_in_threadpool

    llm_executor = _get("llm_executor", ["execute_llm_with_retry"], lambda: execute_llm_with_retry)
    tool_executor = _get("tool_executor", ["execute_mcp_tool"], lambda: execute_mcp_tool)
    history_fetcher = _get("history_fetcher", ["fetch_thread_history"], lambda: fetch_thread_history)
    files_fetcher = _get("files_fetcher", ["fetch_thread_files"], lambda: fetch_thread_files)
    download_file_fn = _get("download_file_fn", ["download_slack_file"], lambda: download_slack_file)
    token_recorder = _get("token_recorder", ["record_thread_tokens"], lambda: record_thread_tokens)
    notes_updater = _get("notes_updater", ["trigger_thread_notes_update"], lambda: trigger_thread_notes_update)
    usage_updater = _get("usage_updater", ["trigger_usage_update"], lambda: trigger_usage_update)
    calc_costs_fn = _get("calc_costs_fn", ["calculate_thread_costs"], lambda: calculate_thread_costs)
    flat_history_recorder = _get("flat_history_recorder", ["record_flat_history", "async_record_flat_history"], lambda: async_record_flat_history)
    sleep_fn = _get("sleep_fn", ["_sleep"], lambda: asyncio.sleep)
    post_system_message_fn = _get("post_system_message_fn", ["post_system_message"], lambda: post_system_message)
    update_message_fn = _get("update_message_fn", ["update_slack_message", "update_agent_message"], lambda: update_slack_message)
    slack_poster = _get("slack_poster", ["post_agent_response"], lambda: post_agent_response)
    run_in_threadpool_fn = _get("run_in_threadpool_fn", ["run_in_threadpool"], lambda: run_in_threadpool)

    # Workspace Adapter Port (Hexagonal Architecture)
    ws_adapter = _get("workspace_adapter")
    if ws_adapter is None:
        from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

        ws_adapter = SlackWorkspaceAdapter(
            post_agent_fn=slack_poster,
            update_msg_fn=update_message_fn,
            post_system_fn=post_system_message_fn,
        )

    debate_turn_pacing = _get("debate_turn_pacing_seconds", ["DEBATE_TURN_PACING_SECONDS"])
    slack_post_pause = _get("slack_post_pause_seconds", ["SLACK_POST_PAUSE_SECONDS"])
    max_tool_iters = _get("max_tool_iterations", ["MAX_TOOL_ITERATIONS", "max_tool_iterations"])
    timeout_s = _get("timeout_secs", ["TIMEOUT_SECS", "TOOL_CALL_LOOP_TIMEOUT_SECONDS", "tool_call_loop_timeout_seconds"])
    intermediate_delay_s = _get("intermediate_delay_secs", ["INTERMEDIATE_DELAY_SECS", "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS", "intermediate_delay_seconds"])

    return PipelineDeps(
        llm_executor=llm_executor,
        tool_executor=tool_executor,
        workspace_adapter=ws_adapter,
        history_fetcher=history_fetcher,
        files_fetcher=files_fetcher,
        download_file_fn=download_file_fn,
        token_recorder=token_recorder,
        notes_updater=notes_updater,
        usage_updater=usage_updater,
        calc_costs_fn=calc_costs_fn,
        flat_history_recorder=flat_history_recorder,
        sleep_fn=sleep_fn,
        post_system_message_fn=post_system_message_fn,
        update_message_fn=update_message_fn,
        slack_poster=slack_poster,
        run_in_threadpool_fn=run_in_threadpool_fn,
        debate_turn_pacing_seconds=debate_turn_pacing,
        slack_post_pause_seconds=slack_post_pause,
        max_tool_iterations=max_tool_iters,
        timeout_secs=timeout_s,
        intermediate_delay_secs=intermediate_delay_s,
    )


__all__ = [
    "CostCalculator",
    "FilesFetcher",
    "FlatHistoryRecorder",
    "HistoryFetcher",
    "LLMExecutor",
    "NotesUpdater",
    "PipelineDeps",
    "SlackMessageUpdater",
    "SlackPoster",
    "SleepFunction",
    "SystemMessagePoster",
    "TokenRecorder",
    "ToolExecutor",
    "UsageUpdater",
    "WorkspaceAdapter",
    "resolve_pipeline_deps",
]



