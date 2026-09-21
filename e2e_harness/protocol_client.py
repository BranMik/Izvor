"""
Slack Protocol Simulation Client.

Simulates authentic Slack Events API and Block Kit Interactivity requests
with cryptographic HMAC-SHA256 signatures, capturing all outbound Slack API calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from core.config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
from main import app


@dataclass
class OutboundEventBus:
    """In-memory event bus tracking all outbound Slack calls made by the application."""

    posted_messages: list[dict[str, Any]] = field(default_factory=list)
    updated_messages: list[dict[str, Any]] = field(default_factory=list)
    system_messages: list[dict[str, Any]] = field(default_factory=list)
    ephemeral_messages: list[dict[str, Any]] = field(default_factory=list)
    uploaded_files: list[dict[str, Any]] = field(default_factory=list)
    published_views: list[dict[str, Any]] = field(default_factory=list)
    usage_messages: list[dict[str, Any]] = field(default_factory=list)
    thread_notes: list[dict[str, Any]] = field(default_factory=list)
    attached_thread_files: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    thread_history_store: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)

    def clear(self) -> None:
        self.posted_messages.clear()
        self.updated_messages.clear()
        self.system_messages.clear()
        self.ephemeral_messages.clear()
        self.uploaded_files.clear()
        self.published_views.clear()
        self.usage_messages.clear()
        self.thread_notes.clear()
        self.attached_thread_files.clear()
        self.thread_history_store.clear()

    def get_last_posted(self) -> dict[str, Any] | None:
        return self.posted_messages[-1] if self.posted_messages else None

    def get_last_system_message(self) -> dict[str, Any] | None:
        return self.system_messages[-1] if self.system_messages else None

    def get_messages_for_thread(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        return [
            m for m in self.posted_messages
            if m.get("channel_id") == channel_id and m.get("thread_ts") == thread_ts
        ]

    def get_all_texts_for_thread(self, channel_id: str, thread_ts: str) -> list[str]:
        texts = []
        for m in self.get_messages_for_thread(channel_id, thread_ts):
            if "text" in m:
                texts.append(m["text"])
        for s in self.system_messages:
            if s.get("channel_id") == channel_id and s.get("thread_ts") == thread_ts:
                texts.append(s["text"])
        return texts


class SlackProtocolClient:
    """High-fidelity client to simulate human actions in Slack against the FastAPI app."""

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_user_id: str = "U012345678",
    ) -> None:
        self.signing_secret = (
            signing_secret
            or SLACK_SIGNING_SECRET
            or "test-signing-secret-key-32-chars-long"
        )
        self.bot_user_id = bot_user_id
        self.bus = OutboundEventBus()
        self.client = TestClient(app)
        self._msg_ts_counter = 1700000000.0

    def next_ts(self) -> str:
        self._msg_ts_counter += 1.0
        return f"{self._msg_ts_counter:.4f}"

    def sign_body(self, body_bytes: bytes, timestamp: str | None = None) -> dict[str, str]:
        """Generates authentic Slack v0 signature headers using HMAC-SHA256."""
        ts = timestamp or str(int(time.time()))
        sig_basestring = f"v0:{ts}:{body_bytes.decode('utf-8')}"
        signature = "v0=" + hmac.new(
            self.signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": signature,
        }

    # Outbound Slack API Mocks
    async def mock_post_agent_response(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]:
        ts = self.next_ts()
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "ts": ts,
            "text": text,
            "username": username or "Agent",
            "icon_url": icon_url,
        }
        self.bus.posted_messages.append(entry)
        return entry

    async def mock_update_slack_message(
        self,
        channel_id: str,
        ts: str,
        text: str,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "ts": ts,
            "text": text,
        }
        self.bus.updated_messages.append(entry)
        for msg in self.bus.posted_messages:
            if msg.get("ts") == ts:
                msg["text"] = text
        return entry

    async def mock_post_system_message(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]:
        ts = self.next_ts()
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "ts": ts,
            "text": text,
            "username": username or "Panel System",
            "icon_url": icon_url,
        }
        self.bus.system_messages.append(entry)
        return entry

    async def mock_post_ephemeral(
        self,
        channel_id: str,
        user_id: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "user_id": user_id,
            "text": text,
            "thread_ts": thread_ts,
        }
        self.bus.ephemeral_messages.append(entry)
        return entry

    async def mock_upload_file(
        self,
        channel_id: str,
        content: str,
        filename: str,
        title: str | None = None,
        initial_comment: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "content": content,
            "filename": filename,
            "title": title,
            "initial_comment": initial_comment,
        }
        self.bus.uploaded_files.append(entry)
        return entry

    async def mock_publish_app_home(
        self,
        user_id: str,
        view: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        entry = {
            "ok": True,
            "user_id": user_id,
            "view": view or kwargs.get("view", {}),
        }
        self.bus.published_views.append(entry)
        return entry

    async def mock_fetch_thread_history(
        self,
        channel_id: str,
        thread_ts: str,
        bot_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.bus.thread_history_store.get((channel_id, thread_ts), [])

    async def mock_fetch_thread_files(
        self,
        channel_id: str,
        thread_ts: str,
    ) -> list[dict[str, Any]]:
        return self.bus.attached_thread_files.get((channel_id, thread_ts), [])

    def attach_file_to_thread(
        self,
        channel_id: str,
        thread_ts: str,
        file_name: str,
        content: str,
        size: int | None = None,
    ) -> None:
        """Simulates a file previously uploaded into a Slack thread."""
        files = self.bus.attached_thread_files.setdefault((channel_id, thread_ts), [])
        files.append({
            "id": f"F_{file_name}",
            "name": file_name,
            "size": size or len(content.encode("utf-8")),
            "mimetype": "text/plain",
            "url_private_download": f"https://slack.local/files/{file_name}",
            "content": content,
            "is_resource_file": False,
        })

    # Inbound Slack Actions Simulator
    def send_event(
        self,
        event_type: str,
        text: str,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str | None = None,
        user_id: str = "U_USER_TESTER",
    ) -> Any:
        """Simulates sending a Slack message / app_mention event via POST /slack/events."""
        ts = self.next_ts()
        actual_thread_ts = thread_ts or ts
        payload = {
            "token": "verification-token",
            "team_id": "T_TEST_TEAM",
            "api_app_id": "A_TEST_APP",
            "type": "event_callback",
            "event": {
                "type": event_type,
                "user": user_id,
                "text": text,
                "ts": ts,
                "channel": channel_id,
                "thread_ts": actual_thread_ts if thread_ts else None,
            },
            "authorizations": [
                {
                    "enterprise_id": None,
                    "team_id": "T_TEST_TEAM",
                    "user_id": self.bot_user_id,
                    "is_bot": True,
                }
            ],
            "event_id": f"Ev_{ts}",
            "event_time": int(time.time()),
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self.sign_body(body_bytes),
        }
        return self.client.post("/slack/events", content=body_bytes, headers=headers)

    def send_mention(
        self,
        text: str,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str | None = None,
        user_id: str = "U_USER_TESTER",
    ) -> Any:
        """Simulates an app_mention event directed at the bot."""
        mention_text = f"<@{self.bot_user_id}> {text}"
        return self.send_event(
            event_type="app_mention",
            text=mention_text,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )

    def send_block_action(
        self,
        action_id: str,
        value: str | None = None,
        selected_option_value: str | None = None,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str = "1700000000.0001",
        user_id: str = "U_USER_TESTER",
        view_state: dict[str, Any] | None = None,
    ) -> Any:
        """Simulates an interactive Block Kit button click or toggle via POST /slack/actions."""
        action_dict: dict[str, Any] = {
            "action_id": action_id,
            "block_id": "test_block",
            "type": "button",
            "action_ts": str(time.time()),
        }
        if value is not None:
            action_dict["value"] = value
        if selected_option_value is not None:
            action_dict["selected_option"] = {"value": selected_option_value}

        payload = {
            "type": "block_actions",
            "user": {"id": user_id, "username": "test_user"},
            "channel": {"id": channel_id},
            "container": {"thread_ts": thread_ts},
            "actions": [action_dict],
            "view": {
                "user": {"id": user_id},
                "state": {"values": view_state or {}},
            },
        }
        form_body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            **self.sign_body(form_body),
        }
        return self.client.post("/slack/actions", content=form_body, headers=headers)
