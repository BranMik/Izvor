"""
Slack Protocol Simulation Client.

Simulates authentic Slack Events API and Block Kit Interactivity requests
with cryptographic HMAC-SHA256 signatures, capturing all outbound Slack API calls.
Supports dual-mode execution:
- Offline Simulation: intercepts and mocks all calls in-memory
- Live Slack Dispatch: broadcasts authentic Block Kit messages, personas, and updates
  to a live Slack test channel while recording events for deterministic assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from core.config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
from main import app
import services.slack_client as _sc

logger = logging.getLogger("e2e_harness.protocol_client")

# Preserve references to real Slack API dispatch methods prior to any mock patching
_REAL_SLACK_POST_AGENT = _sc.post_agent_response
_REAL_SLACK_UPDATE = _sc.update_slack_message
_REAL_SLACK_POST_SYSTEM = _sc.post_system_message
_REAL_SLACK_UPLOAD_FILE = _sc.upload_text_file_to_slack
_REAL_SLACK_POST_EPHEMERAL = _sc.post_ephemeral_to_slack
_REAL_SLACK_PUBLISH_HOME = _sc.publish_app_home


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
        live_mode: bool = False,
        live_channel_id: str | None = None,
    ) -> None:
        self.signing_secret = (
            signing_secret
            or SLACK_SIGNING_SECRET
            or "test-signing-secret-key-32-chars-long"
        )
        self.bot_user_id = bot_user_id
        self.live_mode = live_mode
        self.live_channel_id = live_channel_id
        self.current_thread_ts: str | None = None
        self.current_scenario_tag: str | None = None
        self.bus = OutboundEventBus()
        self.client = TestClient(app)
        self._msg_ts_counter = 1700000000.0

    def get_channel(self, default: str = "C_TEST_DEFAULT") -> str:
        if self.live_mode and self.live_channel_id:
            return self.live_channel_id
        return default

    def get_thread_ts(self, default: str | None = None) -> str | None:
        if self.live_mode and self.current_thread_ts:
            return self.current_thread_ts
        return default

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

    # Live Scenario Management Lifecycle
    async def start_live_scenario(self, scenario_tag: str, scenario_name: str) -> str | None:
        """Creates a dedicated root card in the live Slack channel for this test scenario."""
        if not self.live_mode or not self.live_channel_id:
            return None

        text = (
            f"🧪 *{scenario_tag}: {scenario_name}*\n"
            f"> _Automated behavioural test scenario started. Deliberation turns and responses will stream in this thread._"
        )
        try:
            res = await _REAL_SLACK_POST_SYSTEM(
                channel_id=self.live_channel_id,
                thread_ts="",
                text=text,
                username="E2E Test Runner",
                icon_url="static/v2/avatar_47.png",
            )
            if res and res.get("ok"):
                self.current_thread_ts = str(res.get("ts"))
                self.current_scenario_tag = scenario_tag
                logger.info(f"[LIVE SLACK] Created root thread for {scenario_tag}: ts={self.current_thread_ts}")
                return self.current_thread_ts
            else:
                logger.warning(f"[LIVE SLACK] Could not post scenario root message: {res}")
        except Exception as e:
            logger.error(f"[LIVE SLACK] Error creating scenario root thread: {e}")
        return None

    async def finish_live_scenario(self, result: Any) -> None:
        """Posts final test execution verdict card into the live Slack thread."""
        if not self.live_mode or not self.live_channel_id or not self.current_thread_ts:
            return

        icon = "✅" if result.passed else "❌"
        verdict = "PASSED" if result.passed else "FAILED"
        text = (
            f"{icon} *{self.current_scenario_tag} {verdict}* ({result.duration_seconds:.2f}s)\n"
            f"> *Summary:* {result.details}"
        )
        try:
            await _REAL_SLACK_POST_SYSTEM(
                channel_id=self.live_channel_id,
                thread_ts=self.current_thread_ts,
                text=text,
                username="E2E Test Runner",
                icon_url="static/v2/avatar_47.png",
            )
        except Exception as e:
            logger.error(f"[LIVE SLACK] Error posting scenario completion: {e}")
        finally:
            self.current_thread_ts = None
            self.current_scenario_tag = None

    # Outbound Slack API Handlers (Dual-Mode: records locally and optionally dispatches live)
    async def mock_post_agent_response(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
    ) -> dict[str, Any]:
        real_result = None
        if self.live_mode and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token":
            try:
                real_result = await _REAL_SLACK_POST_AGENT(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                    username=username or "Agent",
                    icon_url=icon_url,
                )
            except Exception as e:
                logger.error(f"[LIVE SLACK] Failed to post agent response: {e}")

        ts = (str(real_result.get("ts")) if real_result and real_result.get("ok") and real_result.get("ts") else None) or self.next_ts()
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "ts": ts,
            "text": text,
            "username": username or "Agent",
            "icon_url": icon_url,
            "raw_slack_response": real_result,
        }
        self.bus.posted_messages.append(entry)
        return entry

    async def mock_update_slack_message(
        self,
        channel_id: str,
        ts: str,
        text: str,
    ) -> dict[str, Any]:
        real_result = None
        if self.live_mode and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token":
            try:
                real_result = await _REAL_SLACK_UPDATE(channel_id=channel_id, ts=ts, text=text)
            except Exception as e:
                logger.error(f"[LIVE SLACK] Failed to update slack message: {e}")

        entry = {
            "ok": True,
            "channel_id": channel_id,
            "ts": ts,
            "text": text,
            "raw_slack_response": real_result,
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
        real_result = None
        if self.live_mode and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token":
            try:
                real_result = await _REAL_SLACK_POST_SYSTEM(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                    username=username,
                    icon_url=icon_url,
                )
            except Exception as e:
                logger.error(f"[LIVE SLACK] Failed to post system message: {e}")

        ts = (str(real_result.get("ts")) if real_result and real_result.get("ok") and real_result.get("ts") else None) or self.next_ts()
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "ts": ts,
            "text": text,
            "username": username or "Panel System",
            "icon_url": icon_url,
            "raw_slack_response": real_result,
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
        real_result = None
        target_thread = thread_ts or self.current_thread_ts
        if self.live_mode and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token":
            try:
                real_result = await _REAL_SLACK_POST_EPHEMERAL(
                    channel_id=channel_id,
                    user_id=user_id,
                    text=text,
                    thread_ts=target_thread,
                )
                if real_result and not real_result.get("ok"):
                    # Fallback so live observers in channel can read the ephemeral notification
                    await _REAL_SLACK_POST_SYSTEM(
                        channel_id=channel_id,
                        thread_ts=target_thread or "",
                        text=f"🔒 _[Ephemeral Notification for {user_id}]:_ {text}",
                    )
            except Exception as e:
                logger.warning(f"[LIVE SLACK] Ephemeral post failed, posting as thread note: {e}")
                try:
                    await _REAL_SLACK_POST_SYSTEM(
                        channel_id=channel_id,
                        thread_ts=target_thread or "",
                        text=f"🔒 _[Ephemeral Notification for {user_id}]:_ {text}",
                    )
                except Exception:
                    pass

        entry = {
            "ok": True,
            "channel_id": channel_id,
            "user_id": user_id,
            "text": text,
            "thread_ts": thread_ts,
            "raw_slack_response": real_result,
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
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        real_result = None
        target_thread = thread_ts or self.current_thread_ts
        if self.live_mode and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token":
            try:
                real_result = await _REAL_SLACK_UPLOAD_FILE(
                    channel_id=channel_id,
                    content=content,
                    filename=filename,
                    title=title,
                    initial_comment=initial_comment,
                    thread_ts=target_thread,
                )
            except Exception as e:
                logger.error(f"[LIVE SLACK] Failed to upload file to live Slack: {e}")

        entry = {
            "ok": True,
            "channel_id": channel_id,
            "content": content,
            "filename": filename,
            "title": title,
            "initial_comment": initial_comment,
            "thread_ts": thread_ts,
            "raw_slack_response": real_result,
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

    async def send_mention(
        self,
        text: str,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str | None = None,
        user_id: str = "U_USER_TESTER",
    ) -> Any:
        """Simulates an app_mention event directed at the bot, optionally broadcasting to live Slack."""
        target_channel = self.get_channel(channel_id)
        target_thread = self.get_thread_ts(thread_ts)

        if self.live_mode and self.live_channel_id and target_thread:
            try:
                await _REAL_SLACK_POST_SYSTEM(
                    channel_id=target_channel,
                    thread_ts=target_thread,
                    text=f"👤 *Tester Prompt:*\n> {text}",
                    username="Tester",
                    icon_url="static/v2/avatar_37.png",
                )
            except Exception as e:
                logger.warning(f"[LIVE SLACK] Failed to post prompt to thread: {e}")

        mention_text = f"<@{self.bot_user_id}> {text}"
        return self.send_event(
            event_type="app_mention",
            text=mention_text,
            channel_id=target_channel,
            thread_ts=target_thread,
            user_id=user_id,
        )

    async def send_block_action(
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
        target_channel = self.get_channel(channel_id)
        target_thread = self.get_thread_ts(thread_ts) or thread_ts

        if self.live_mode and self.live_channel_id and target_thread:
            try:
                await _REAL_SLACK_POST_SYSTEM(
                    channel_id=target_channel,
                    thread_ts=target_thread,
                    text=f"🖱️ *User Action Clicked:* `{action_id}` (value: `{value}`)",
                    username="Tester",
                    icon_url="static/v2/avatar_37.png",
                )
            except Exception as e:
                logger.warning(f"[LIVE SLACK] Failed to post action notification to thread: {e}")

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
            "channel": {"id": target_channel},
            "container": {"thread_ts": target_thread},
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
