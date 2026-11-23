from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from core.registry import get_default_workspace_adapter_factory, get_skill_registry



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
        allow_503_immediate_retry: bool = True,
        turn_token_mapping: dict[str, str] | None = None,
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
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        channel_name: str | None = None,
    ) -> Any: ...


@runtime_checkable
class NotesUpdater(Protocol):
    """Triggers background updates to thread notes summary."""

    def __call__(
        self,
        channel_id: str,
        thread_ts: str,
        is_completed: bool = False,
        bot_user_id: str | None = None,
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
        ts: str,
        text: str,
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
        self,
        channel_id: str,
        thread_ts: str,
        initial_prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> Any: ...


def create_default_workspace_adapter(
    *,
    post_agent_fn: Any = None,
    update_msg_fn: Any = None,
    post_system_fn: Any = None,
) -> WorkspaceAdapter:
    """Constructs the registered default WorkspaceAdapter without upward adapter imports."""
    factory = get_default_workspace_adapter_factory()
    if factory is None:
        raise RuntimeError(
            "Default WorkspaceAdapter factory is not configured. Call set_default_workspace_adapter_factory()."
        )
    return factory(
        post_agent_fn=post_agent_fn,
        update_msg_fn=update_msg_fn,
        post_system_fn=post_system_fn,
    )


def _first_not_none(*values: Any) -> Any:
    """Returns the first non-None value (preserving falsy 0 / 0.0 numeric overrides)."""
    for v in values:
        if v is not None:
            return v
    return None


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
    download_file_fn: Any = None
    debate_turn_pacing_seconds: float | None = None
    slack_post_pause_seconds: float | None = None
    max_tool_iterations: int | None = None
    timeout_secs: float | None = None
    intermediate_delay_secs: float | None = None
    skill_registry: Any = None
    spend_governance: Any = None


def resolve_pipeline_deps(
    deps: PipelineDeps | None = None,
    **overrides: Any,
) -> PipelineDeps:
    """
    Constructs a fully populated, immutable PipelineDeps instance.
    Injected arguments take highest precedence, followed by production defaults.
    """
    # Helper to resolve field value: overrides -> deps -> lazy production default
    def _get(field_name: str, default_factory: Callable[[], Any] | None = None) -> Any:
        if field_name in overrides and overrides[field_name] is not None:
            return overrides[field_name]
        if deps is not None and getattr(deps, field_name, None) is not None:
            return getattr(deps, field_name)
        if default_factory is not None:
            return default_factory()
        return None

    from core.llm_utils import execute_llm_with_retry
    from services.local_tools import execute_local_tool
    from services.slack import (
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

    llm_executor = (
        _get("llm_executor")
        or _get("execute_llm_with_retry")
        or execute_llm_with_retry
    )
    tool_executor = (
        _get("tool_executor")
        or _get("execute_local_tool")
        or execute_local_tool
    )
    history_fetcher = _get("history_fetcher", default_factory=lambda: fetch_thread_history)
    files_fetcher = _get("files_fetcher", default_factory=lambda: fetch_thread_files)
    download_file_fn = _get("download_file_fn", default_factory=lambda: download_slack_file)
    token_recorder = _get("token_recorder", default_factory=lambda: record_thread_tokens)
    notes_updater = _get("notes_updater", default_factory=lambda: trigger_thread_notes_update)
    usage_updater = _get("usage_updater", default_factory=lambda: trigger_usage_update)
    calc_costs_fn = _get("calc_costs_fn", default_factory=lambda: calculate_thread_costs)
    flat_history_recorder = _get("flat_history_recorder", default_factory=lambda: async_record_flat_history)
    sleep_fn = _get("sleep_fn") or _get("asyncio_sleep") or asyncio.sleep
    post_system_message_fn = (
        _get("post_system_message_fn")
        or _get("post_system_message")
        or post_system_message
    )
    update_message_fn = (
        _get("update_message_fn")
        or _get("update_slack_message")
        or update_slack_message
    )
    slack_poster = (
        _get("slack_poster")
        or _get("post_agent_response")
        or _get("post_agent_response_fn")
        or post_agent_response
    )

    # Workspace Adapter Port (Hexagonal Architecture)
    ws_adapter = _get("workspace_adapter")
    if ws_adapter is None:
        ws_adapter = create_default_workspace_adapter(
            post_agent_fn=slack_poster,
            update_msg_fn=update_message_fn,
            post_system_fn=post_system_message_fn,
        )

    debate_turn_pacing = _first_not_none(
        _get("debate_turn_pacing_seconds"),
        _get("DEBATE_TURN_PACING_SECONDS"),
    )
    slack_post_pause = _first_not_none(
        _get("slack_post_pause_seconds"),
        _get("SLACK_POST_PAUSE_SECONDS"),
    )
    max_tool_iters = _first_not_none(
        _get("max_tool_iterations"),
        _get("MAX_TOOL_ITERATIONS"),
    )
    timeout_s = _first_not_none(
        _get("timeout_secs"),
        _get("TOOL_CALL_LOOP_TIMEOUT_SECONDS"),
        _get("timeout_seconds"),
    )
    intermediate_delay_s = _first_not_none(
        _get("intermediate_delay_secs"),
        _get("TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS"),
        _get("intermediate_delay_seconds"),
    )

    skill_reg = _get("skill_registry")
    if skill_reg is None:
        try:
            skill_reg = get_skill_registry()
        except RuntimeError:
            import importlib

            importlib.import_module("skills.registry")
            skill_reg = get_skill_registry()

    spend_gov = _get("spend_governance")
    if spend_gov is None:
        from pipeline.spend_governance import spend_governance as default_spend_governance

        spend_gov = default_spend_governance

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
        debate_turn_pacing_seconds=debate_turn_pacing,
        slack_post_pause_seconds=slack_post_pause,
        max_tool_iterations=max_tool_iters,
        timeout_secs=timeout_s,
        intermediate_delay_secs=intermediate_delay_s,
        skill_registry=skill_reg,
        spend_governance=spend_gov,
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
    "create_default_workspace_adapter",
    "resolve_pipeline_deps",
]



