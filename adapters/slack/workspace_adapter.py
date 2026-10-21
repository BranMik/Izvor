"""
Hexagonal workspace adapter implementations.
Decouples core orchestration stages from chat-platform primitives (Slack vs In-Memory/CLI).
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable

from nlp.message_builder import markdown_to_slack
from orchestration.protocols import WorkspaceAdapter
from services.slack_client import (
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
        hook_resolver: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._post_agent_fn = post_agent_fn
        self._update_msg_fn = update_msg_fn
        self._post_system_fn = post_system_fn
        self._hook_resolver = hook_resolver

    def _resolve_post_fn(self) -> Callable[..., Any]:
        if self._post_agent_fn is not None:
            return self._post_agent_fn
        if self._hook_resolver is not None:
            return self._hook_resolver("post_agent_response", post_agent_response)
        return post_agent_response

    def _resolve_update_fn(self) -> Callable[..., Any]:
        if self._update_msg_fn is not None:
            return self._update_msg_fn
        if self._hook_resolver is not None:
            return self._hook_resolver("update_slack_message", update_slack_message)
        return update_slack_message

    def _resolve_system_fn(self) -> Callable[..., Any]:
        if self._post_system_fn is not None:
            return self._post_system_fn
        if self._hook_resolver is not None:
            return self._hook_resolver("post_system_message", post_system_message)
        return post_system_message

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


class InMemoryWorkspaceAdapter:
    """Pure in-memory WorkspaceAdapter for testing and channel-agnostic execution without Slack."""

    def __init__(self) -> None:
        self.posted_turns: list[dict[str, Any]] = []
        self.updated_turns: list[dict[str, Any]] = []
        self.system_notices: list[dict[str, Any]] = []

    def format_outbound_text(self, markdown_text: str) -> str:
        """In-memory returns plain markdown text without Slack formatting transformations."""
        return markdown_text

    def build_budget_halt_blocks(
        self, message_text: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Constructs platform-agnostic notice blocks."""
        return [
            {
                "type": "notice",
                "text": message_text,
                "session_id": session_id,
                "action": "resume_sequence",
            }
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
        ts = f"mock_turn_ts_{time.time()}_{len(self.posted_turns) + 1}"
        record = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text": text,
            "username": username,
            "icon_url": icon_url,
            "blocks": blocks,
            "ts": ts,
        }
        self.posted_turns.append(record)
        return {"ok": True, "ts": ts, "message": record}

    async def update_agent_turn(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        record = {
            "channel_id": channel_id,
            "message_ts": message_ts,
            "text": text,
            "blocks": blocks,
            "updated_at": time.time(),
        }
        self.updated_turns.append(record)
        return {"ok": True, "ts": message_ts, "message": record}

    async def post_system_notice(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]:
        ts = f"mock_sys_ts_{time.time()}_{len(self.system_notices) + 1}"
        record = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text": text,
            "username": username,
            "icon_url": icon_url,
            "ts": ts,
        }
        self.system_notices.append(record)
        return {"ok": True, "ts": ts, "message": record}
