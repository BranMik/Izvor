"""
In-memory WorkspaceAdapter test double for channel-agnostic test execution.
"""

from __future__ import annotations

import time
from typing import Any


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

