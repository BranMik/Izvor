"""
Hexagonal workspace adapter implementations.
Decouples core orchestration stages from chat-platform primitives (Slack vs In-Memory/CLI).
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from core.registry import set_default_workspace_adapter_factory
from nlp.message_builder import markdown_to_slack
from services.slack import (
    post_agent_response,
    post_system_message,
    update_slack_message,
)

logger = logging.getLogger(__name__)


class SlackWorkspaceAdapter:
    """Concrete Slack implementation of the WorkspaceAdapter port."""

    def __init__(
        self,
        *,
        post_agent_fn: Callable[..., Any] | None = None,
        update_msg_fn: Callable[..., Any] | None = None,
        post_system_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._post_agent_fn = post_agent_fn
        self._update_msg_fn = update_msg_fn
        self._post_system_fn = post_system_fn

    def _resolve_post_fn(self) -> Callable[..., Any]:
        return self._post_agent_fn or post_agent_response

    def _resolve_update_fn(self) -> Callable[..., Any]:
        return self._update_msg_fn or update_slack_message

    def _resolve_system_fn(self) -> Callable[..., Any]:
        return self._post_system_fn or post_system_message


    def format_outbound_text(self, markdown_text: str) -> str:
        """Translates standard markdown to Slack mrkdwn formatting."""
        return markdown_to_slack(markdown_text)

    def build_budget_halt_blocks(
        self, message_text: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Constructs Slack Block Kit cards for budget / circuit breaker pause."""
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message_text},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Continue Sequence"},
                        "action_id": "resume_sequence_action",
                        "style": "primary",
                        "value": session_id,
                    }
                ],
            },
        ]

    async def post_agent_turn(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str,
        icon_url: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Posts an agent response to a Slack thread using dynamic persona customization."""
        fn = self._resolve_post_fn()
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text": text,
            "username": username,
            "icon_url": icon_url,
        }
        if blocks is not None:
            kwargs["blocks"] = blocks
        res = fn(**kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    async def update_agent_turn(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Updates an existing Slack message in-place."""
        fn = self._resolve_update_fn()
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "ts": message_ts,
            "text": text,
        }
        if blocks is not None:
            kwargs["blocks"] = blocks
        res = fn(**kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

    async def post_system_notice(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]:
        """Posts a system notice or administrative alert to a Slack thread."""
        fn = self._resolve_system_fn()
        res = fn(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_url=icon_url,
        )
        if inspect.isawaitable(res):
            return await res
        return res


set_default_workspace_adapter_factory(SlackWorkspaceAdapter)

