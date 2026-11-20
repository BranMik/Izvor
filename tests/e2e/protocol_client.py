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
import contextvars
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
from slack_sdk.errors import SlackApiError

import services.slack as _sc
from services.slack.client_core import _raw_slack_async_client
from core.config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, resolve_avatar_url
from main import app

logger = logging.getLogger("tests.e2e.protocol_client")

TEST_ADMIN_USER_ID = "U_ADMIN_TESTER"
TEST_USER_ID = "U_USER_TESTER"

_SUPPRESS_SPY_POST_RECORD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_SUPPRESS_SPY_POST_RECORD", default=False
)


def _is_synthetic_e2e_id(value: str) -> bool:
    """Returns True for fake E2E user/DM identifiers that do not exist in a real Slack workspace."""
    cleaned = str(value or "").strip()
    return cleaned.startswith(("U_TESTER", "D_TEST_DM")) or cleaned in {
        TEST_USER_ID,
        TEST_ADMIN_USER_ID,
    }

# Preserve references to real Slack API dispatch methods prior to any mock patching
_REAL_SLACK_POST_AGENT = _sc.post_agent_response
_REAL_SLACK_UPDATE = _sc.update_slack_message
_REAL_SLACK_POST_SYSTEM = _sc.post_system_message
_REAL_SLACK_UPLOAD_FILE = _sc.upload_text_file_to_slack
_REAL_SLACK_POST_EPHEMERAL = _sc.post_ephemeral_to_slack
_REAL_SLACK_PUBLISH_HOME = _sc.publish_app_home
_REAL_SLACK_POST_NOTE = _sc.post_thread_note
_REAL_SLACK_UPDATE_NOTE = _sc.update_thread_note
_REAL_SLACK_FETCH_HISTORY = _sc.fetch_thread_history


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
    opened_dms: list[dict[str, Any]] = field(default_factory=list)
    unpinned_notes: list[dict[str, Any]] = field(default_factory=list)
    hitl_cards: list[dict[str, Any]] = field(default_factory=list)
    run_summaries: list[dict[str, Any]] = field(default_factory=list)
    attached_thread_files: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    thread_history_store: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )

    def clear(self) -> None:
        self.posted_messages.clear()
        self.updated_messages.clear()
        self.system_messages.clear()
        self.ephemeral_messages.clear()
        self.uploaded_files.clear()
        self.published_views.clear()
        self.usage_messages.clear()
        self.thread_notes.clear()
        self.opened_dms.clear()
        self.unpinned_notes.clear()
        self.hitl_cards.clear()
        self.run_summaries.clear()
        self.attached_thread_files.clear()
        self.thread_history_store.clear()

    def get_last_posted(self) -> dict[str, Any] | None:
        return self.posted_messages[-1] if self.posted_messages else None

    def get_last_system_message(self) -> dict[str, Any] | None:
        return self.system_messages[-1] if self.system_messages else None

    def get_messages_for_thread(
        self, channel_id: str, thread_ts: str
    ) -> list[dict[str, Any]]:
        return [
            m
            for m in self.posted_messages
            if m.get("channel_id") == channel_id
            and (not thread_ts or m.get("thread_ts") == thread_ts)
            and m.get("role") != "user"
            and not m.get("is_scribe_note")
            and not m.get("is_usage_card")
            and not m.get("is_hitl_card")
            and m.get("username")
            not in (
                "Live Discussion Scribe",
                "Discussion Conclusion",
                "Izvor Usage Analyst",
                "E2E Test Runner",
                "Panel System",
                "Orchestrator",
            )
        ]

    def get_all_texts_for_thread(self, channel_id: str, thread_ts: str) -> list[str]:
        texts = []
        for m in self.get_messages_for_thread(channel_id, thread_ts):
            if "text" in m:
                texts.append(m["text"])
        for s in self.system_messages:
            if s.get("channel_id") == channel_id and s.get("thread_ts") == thread_ts:
                texts.append(s["text"])
        for h in self.hitl_cards:
            if h.get("channel_id") == channel_id and h.get("thread_ts") == thread_ts:
                texts.append(h["text"])
        return texts


class MockSlackResponse(dict):
    """Dict subclass providing `.data` attribute compatibility with slack_sdk AsyncSlackResponse."""

    @property
    def data(self) -> dict[str, Any]:
        return self


class MockSlackAsyncClient:
    """In-memory Slack AsyncWebClient double recording outbound Slack API calls onto OutboundEventBus."""

    def __init__(self, protocol_client: "SlackProtocolClient") -> None:
        self._client = protocol_client
        self.fail_dm_open: str | None = None
        self.fail_file_upload: str | None = None

    def reset_faults(self) -> None:
        self.fail_dm_open = None
        self.fail_file_upload = None

    async def _forward_live(self, method_name: str, **kwargs: Any) -> Any:
        """Forwards Slack Web API calls to the real AsyncWebClient when live_mode is enabled."""
        bot_token = self._client.bot_token
        if (
            self._client.live_mode
            and bot_token
            and bot_token != "xoxb-your-bot-user-oauth-token"
        ):
            fn = getattr(self._client.raw_slack_client, method_name, None)
            if fn is not None:
                return await fn(**kwargs)
        return None

    async def chat_postMessage(self, **kwargs: Any) -> MockSlackResponse:
        channel_id = str(kwargs.get("channel", ""))
        thread_ts = str(kwargs.get("thread_ts") or self._client.current_thread_ts or "")
        text = str(kwargs.get("text", ""))
        username = kwargs.get("username")
        blocks = kwargs.get("blocks")
        live_ts = None
        try:
            live_res = await self._forward_live("chat_postMessage", **kwargs)
            if live_res and live_res.get("ok") and live_res.get("ts"):
                live_ts = str(live_res.get("ts"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LIVE SLACK SPY] chat_postMessage failed: {e}")

        ts = live_ts or self._client.next_ts()

        blocks_str = json.dumps(blocks or [])
        is_usage = username == "Izvor Usage Analyst" or channel_id.startswith("mock_izvor") or channel_id == "C_MOCK_USAGE"
        is_hitl = "hitl_approve_tool" in blocks_str or "hitl_deny_tool" in blocks_str
        is_scribe = (
            username in ("Live Discussion Scribe", "Discussion Conclusion")
            or "download_scribe_notes_txt" in blocks_str
            or "download_conclusion_notes_txt" in blocks_str
        )

        entry = {
            "ok": True,
            "ts": ts,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text": text,
            "username": username or "Bot",
            "icon_url": kwargs.get("icon_url"),
            "blocks": blocks,
            "is_scribe_note": is_scribe,
            "is_usage_card": is_usage,
            "is_hitl_card": is_hitl,
        }
        if not _SUPPRESS_SPY_POST_RECORD.get():
            if is_usage:
                self._client.bus.usage_messages.append(entry)
            elif is_scribe:
                self._client.bus.thread_notes.append(entry)
                self._client.bus.posted_messages.append(entry)
            elif is_hitl:
                self._client.bus.hitl_cards.append(entry)
                self._client.bus.posted_messages.append(entry)
            else:
                self._client.bus.posted_messages.append(entry)

        return MockSlackResponse({"ok": True, "ts": ts, "channel": channel_id})

    async def chat_update(self, **kwargs: Any) -> MockSlackResponse:
        channel_id = str(kwargs.get("channel", ""))
        ts = str(kwargs.get("ts", ""))
        text = str(kwargs.get("text", ""))
        blocks = kwargs.get("blocks")
        try:
            await self._forward_live("chat_update", **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LIVE SLACK SPY] chat_update failed: {e}")
        entry = {
            "ok": True,
            "channel_id": channel_id,
            "ts": ts,
            "text": text,
            "blocks": blocks,
        }
        self._client.bus.updated_messages.append(entry)
        for coll in (
            self._client.bus.posted_messages,
            self._client.bus.thread_notes,
            self._client.bus.usage_messages,
            self._client.bus.hitl_cards,
        ):
            for msg in coll:
                if msg.get("ts") == ts:
                    msg["text"] = text
                    if blocks is not None:
                        msg["blocks"] = blocks
        return MockSlackResponse({"ok": True, "ts": ts, "channel": channel_id})

    async def conversations_open(self, **kwargs: Any) -> MockSlackResponse:
        users = str(kwargs.get("users", ""))
        if self.fail_dm_open:
            raise SlackApiError(
                f"Slack API error: {self.fail_dm_open}",
                response={"ok": False, "error": self.fail_dm_open},
            )
        live_dm_id = None
        if not _is_synthetic_e2e_id(users):
            try:
                live_res = await self._forward_live("conversations_open", **kwargs)
                if live_res and live_res.get("ok"):
                    live_dm_id = live_res.get("channel", {}).get("id")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LIVE SLACK SPY] conversations_open failed: {e}")
        dm_channel_id = live_dm_id or (f"D_TEST_DM_{users}" if users else "D_TEST_DM")
        self._client.bus.opened_dms.append({"users": users, "channel_id": dm_channel_id})
        return MockSlackResponse({"ok": True, "channel": {"id": dm_channel_id}})

    async def files_upload_v2(self, **kwargs: Any) -> MockSlackResponse:
        if self.fail_file_upload:
            raise SlackApiError(
                f"Slack API error: {self.fail_file_upload}",
                response={"ok": False, "error": self.fail_file_upload},
            )
        channel_id = str(kwargs.get("channel", ""))
        live_file_id = None
        if not _is_synthetic_e2e_id(channel_id):
            try:
                live_res = await self._forward_live("files_upload_v2", **kwargs)
                if live_res and live_res.get("ok"):
                    live_file_id = live_res.get("file", {}).get("id")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LIVE SLACK SPY] files_upload_v2 failed: {e}")
        filename = str(kwargs.get("filename", "upload.txt"))
        content = str(kwargs.get("content", ""))
        file_id = live_file_id or f"F_TEST_{self._client.next_ts()}"
        entry = {
            "ok": True,
            "id": file_id,
            "channel_id": channel_id,
            "content": content,
            "filename": filename,
            "title": kwargs.get("title"),
            "initial_comment": kwargs.get("initial_comment"),
            "thread_ts": kwargs.get("thread_ts"),
        }
        self._client.bus.uploaded_files.append(entry)
        return MockSlackResponse({"ok": True, "file": {"id": file_id, "name": filename}})

    async def chat_postEphemeral(self, **kwargs: Any) -> MockSlackResponse:
        user_id = str(kwargs.get("user", ""))
        if not _is_synthetic_e2e_id(user_id):
            try:
                await self._forward_live("chat_postEphemeral", **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[LIVE SLACK SPY] chat_postEphemeral failed: {e}")
        entry = {
            "ok": True,
            "channel_id": kwargs.get("channel", ""),
            "user_id": user_id,
            "text": kwargs.get("text", ""),
            "thread_ts": kwargs.get("thread_ts"),
        }
        self._client.bus.ephemeral_messages.append(entry)
        return MockSlackResponse({"ok": True})

    async def pins_add(self, **kwargs: Any) -> MockSlackResponse:
        try:
            await self._forward_live("pins_add", **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] pins_add failed: {e}")
        return MockSlackResponse({"ok": True})

    async def pins_remove(self, **kwargs: Any) -> MockSlackResponse:
        try:
            await self._forward_live("pins_remove", **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] pins_remove failed: {e}")
        self._client.bus.unpinned_notes.append(
            {"channel_id": kwargs.get("channel", ""), "timestamp": kwargs.get("timestamp", "")}
        )
        return MockSlackResponse({"ok": True})

    async def views_publish(self, **kwargs: Any) -> MockSlackResponse:
        try:
            await self._forward_live("views_publish", **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] views_publish failed: {e}")
        entry = {
            "ok": True,
            "user_id": kwargs.get("user_id", ""),
            "view": kwargs.get("view", {}),
        }
        self._client.bus.published_views.append(entry)
        return MockSlackResponse({"ok": True})

    async def conversations_info(self, **kwargs: Any) -> MockSlackResponse:
        try:
            live_res = await self._forward_live("conversations_info", **kwargs)
            if live_res and live_res.get("ok"):
                return live_res
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] conversations_info failed: {e}")
        ch = str(kwargs.get("channel", "C_TEST"))
        return MockSlackResponse({"ok": True, "channel": {"id": ch, "name": ch.lower()}})

    async def conversations_list(self, **kwargs: Any) -> MockSlackResponse:
        try:
            live_res = await self._forward_live("conversations_list", **kwargs)
            if live_res and live_res.get("ok"):
                return live_res
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] conversations_list failed: {e}")
        return MockSlackResponse(
            {
                "ok": True,
                "channels": [{"id": "C_MOCK_USAGE", "name": "izvor-usage", "is_member": True}],
                "response_metadata": {"next_cursor": ""},
            }
        )

    async def conversations_replies(self, **kwargs: Any) -> MockSlackResponse:
        try:
            live_res = await self._forward_live("conversations_replies", **kwargs)
            if live_res is not None:
                return live_res
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LIVE SLACK SPY] conversations_replies failed: {e}")
        return MockSlackResponse({"ok": True, "messages": [], "has_more": False})

    def __getattr__(self, name: str) -> Any:
        raw_attr = getattr(self._client.raw_slack_client, name, None)
        if callable(raw_attr):
            async def _delegate(*args: Any, **kwargs: Any) -> Any:
                bot_token = self._client.bot_token
                if (
                    self._client.live_mode
                    and bot_token
                    and bot_token != "xoxb-your-bot-user-oauth-token"
                ):
                    return await raw_attr(*args, **kwargs)
                return MockSlackResponse({"ok": True})

            return _delegate
        raise AttributeError(name)


class SlackProtocolClient:
    """High-fidelity client to simulate human actions in Slack against the FastAPI app."""

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_user_id: str = "U012345678",
        live_mode: bool = False,
        live_channel_id: str | None = None,
        raw_slack_client: Any = None,
        bot_token: str | None = None,
    ) -> None:
        raw_secret = signing_secret or SLACK_SIGNING_SECRET
        if not raw_secret or raw_secret == "your-slack-signing-secret":
            raw_secret = "test-signing-secret-key-32-chars-long"
        self.signing_secret = raw_secret
        self.bot_user_id = bot_user_id
        self.live_mode = live_mode
        self.live_channel_id = live_channel_id
        self.raw_slack_client = raw_slack_client if raw_slack_client is not None else _raw_slack_async_client
        self.bot_token = bot_token if bot_token is not None else SLACK_BOT_TOKEN
        self.current_thread_ts: str | None = None
        self.current_scenario_tag: str | None = None
        self.current_scenario_name: str | None = None
        self.bus = OutboundEventBus()
        self.slack_client_double = MockSlackAsyncClient(self)
        self.async_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )
        self.client = self.async_client
        self._msg_ts_counter = time.time() + 1000.0
        self._dispatched_threads: set[tuple[str, str]] = set()
        self._scenario_live_threads: list[str] = []
        self._scenario_root_messages: dict[str, dict[str, str]] = {}

    async def aclose(self) -> None:
        await self.drain_background_tasks()
        await self.async_client.aclose()

    async def drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Awaits or cancels any active dispatch, scribe, or usage tasks spawned during a scenario."""
        from agents.thread_notes_agent import _RUNNING_TASKS
        from agents.usage_agent import usage_tracker
        from orchestration.thread_state import thread_state_manager

        active_tasks: list[asyncio.Task[Any]] = []
        for ch, th in list(self._dispatched_threads):
            t = thread_state_manager.get_active_task(ch, th)
            if t and not t.done():
                active_tasks.append(t)
        self._dispatched_threads.clear()

        for t in list(_RUNNING_TASKS.values()):
            if t and not t.done():
                active_tasks.append(t)
        for t in list(usage_tracker.running_usage_tasks.values()):
            if t and not t.done():
                active_tasks.append(t)

        if active_tasks:
            done, pending = await asyncio.wait(active_tasks, timeout=timeout)
            for p in pending:
                p.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for d in done:
                if not d.cancelled():
                    d.exception()

        _RUNNING_TASKS.clear()
        usage_tracker.running_usage_tasks.clear()

    async def await_active_task(
        self, channel_id: str, thread_ts: str, timeout: float = 30.0
    ) -> None:
        """Waits for any active background discussion task for (channel_id, thread_ts) to complete."""
        from orchestration.thread_state import thread_state_manager

        deadline = time.time() + timeout
        while time.time() < deadline:
            task = thread_state_manager.get_active_task(channel_id, thread_ts)
            if not task or task.done():
                return
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=max(0.1, deadline - time.time())
                )
                return
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
            except TimeoutError:
                return
            except Exception:  # noqa: BLE001
                return

    def set_active_scenario(self, scenario_tag: str, scenario_name: str) -> None:
        """Prepares metadata for the upcoming test scenario."""
        self.current_scenario_tag = scenario_tag
        self.current_scenario_name = scenario_name
        self.current_thread_ts = None
        self._dispatched_threads.clear()
        self._scenario_live_threads.clear()
        self._scenario_root_messages.clear()

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

    def sign_body(
        self, body_bytes: bytes, timestamp: str | None = None
    ) -> dict[str, str]:
        """Generates authentic Slack v0 signature headers using HMAC-SHA256."""
        ts = timestamp or str(int(time.time()))
        sig_basestring = f"v0:{ts}:{body_bytes.decode('utf-8')}"
        signature = (
            "v0="
            + hmac.new(
                self.signing_secret.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        return {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": signature,
        }

    # Live Scenario Management Lifecycle
    @staticmethod
    def _chunk_summary_lines(lines: list[str], max_chars: int = 2800) -> list[str]:
        """Splits summary lines into Slack-safe chunks on line boundaries."""
        if not lines:
            return []
        chunks: list[str] = []
        current_lines: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + (1 if current_lines else 0)
            if current_lines and (current_len + line_len > max_chars):
                chunks.append("\n".join(current_lines))
                current_lines = [line]
                current_len = len(line)
            else:
                current_lines.append(line)
                current_len += line_len
        if current_lines:
            chunks.append("\n".join(current_lines))
        return chunks

    async def start_live_scenario(self, prompt_text: str | None = None) -> str | None:
        """Creates the initial conversation root card in the live Slack channel containing scenario title and tester prompt."""
        if not self.live_mode or not self.live_channel_id:
            return None

        scenario_tag = self.current_scenario_tag or "TEST"
        scenario_name = self.current_scenario_name or "Scenario"
        clean_prompt = (prompt_text or "").strip()

        title_row = f"🧪 *{scenario_tag}: {scenario_name}*"
        if clean_prompt:
            body_row = f"> | Automated behavioural test scenario: {clean_prompt}"
        else:
            body_row = "> | Automated behavioural test scenario started."
        text = f"{title_row}\n{body_row}"

        try:
            res = await self.raw_slack_client.chat_postMessage(
                channel=self.live_channel_id,
                text=text,
                username="E2E Test Runner",
                icon_url=resolve_avatar_url("static/image_99.png"),
            )
            if res and res.get("ok"):
                self.current_thread_ts = str(res.get("ts"))
                if self.current_thread_ts:
                    self._scenario_root_messages[self.current_thread_ts] = {
                        "title_row": title_row,
                        "body_row": body_row,
                    }
                    if self.current_thread_ts not in self._scenario_live_threads:
                        self._scenario_live_threads.append(self.current_thread_ts)
                logger.info(
                    f"[LIVE SLACK] Created root conversation thread for {scenario_tag}: ts={self.current_thread_ts}"
                )
                return self.current_thread_ts
            else:
                logger.warning(
                    f"[LIVE SLACK] Could not post scenario root message: {res}"
                )
        except Exception as e:  # noqa: BLE001 - Live Slack test scenario root initialization boundary
            logger.error(f"[LIVE SLACK] Error creating scenario root thread: {e}")
        return None

    async def finish_live_scenario(self, result: Any) -> None:
        """Updates initial root messages with first-row PASS/FAIL and posts final verdict cards into all opened Slack threads."""
        target_threads = list(
            dict.fromkeys(
                [t for t in self._scenario_live_threads if t]
                + ([self.current_thread_ts] if self.current_thread_ts else [])
            )
        )
        if not self.live_mode or not self.live_channel_id or not target_threads:
            self._scenario_live_threads.clear()
            self._scenario_root_messages.clear()
            return

        icon = "✅" if result.passed else "❌"
        verdict_short = "PASS" if result.passed else "FAIL"
        verdict_long = "PASSED" if result.passed else "FAILED"
        scenario_tag = self.current_scenario_tag or "TEST"
        scenario_name = self.current_scenario_name or "Scenario"
        default_title_row = f"🧪 *{scenario_tag}: {scenario_name}*"
        reply_text = (
            f"{icon} *{scenario_tag} {verdict_long}* ({result.duration_seconds:.2f}s)\n"
            f"> *Summary:* {result.details}"
        )
        try:
            for target_thread in target_threads:
                root_meta = self._scenario_root_messages.get(target_thread, {})
                title_row = root_meta.get("title_row") or default_title_row
                body_row = root_meta.get("body_row", "")
                updated_first_row = (
                    f"{title_row} — {icon} *{verdict_short}* ({result.duration_seconds:.2f}s)"
                )
                updated_root_text = (
                    f"{updated_first_row}\n{body_row}" if body_row else updated_first_row
                )
                try:
                    await self.raw_slack_client.chat_update(
                        channel=self.live_channel_id,
                        ts=target_thread,
                        text=updated_root_text,
                    )
                except Exception as e:  # noqa: BLE001 - Live Slack root message update boundary
                    logger.error(
                        f"[LIVE SLACK] Error updating root message {target_thread} with {verdict_short}: {e}"
                    )

                try:
                    await self.raw_slack_client.chat_postMessage(
                        channel=self.live_channel_id,
                        thread_ts=target_thread,
                        text=reply_text,
                        username="E2E Test Runner",
                        icon_url=resolve_avatar_url("static/image_99.png"),
                    )
                except Exception as e:  # noqa: BLE001 - Live Slack scenario completion card boundary
                    logger.error(f"[LIVE SLACK] Error posting scenario completion to {target_thread}: {e}")
        finally:
            self._scenario_live_threads.clear()
            self._scenario_root_messages.clear()
            self.current_thread_ts = None
            self.current_scenario_tag = None
            self.current_scenario_name = None

    async def post_live_run_summary(
        self,
        run_prefix: str,
        mode_desc: str,
        llm_mode: str,
        results: list[Any],
        total_duration: float,
    ) -> str | None:
        """Posts a final E2E test suite summary in a separate Slack thread and records it on the bus."""
        total_count = len(results)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = total_count - passed_count
        overall_icon = "✅" if failed_count == 0 else "❌"

        header_text = (
            f"📊 *{run_prefix}: E2E Test Suite Summary* — {overall_icon} "
            f"*{passed_count}/{total_count} PASSED* ({failed_count} FAILED, {total_duration:.2f}s)\n"
            f"> Mode: {mode_desc} | LLM: {llm_mode} | Scenarios: {total_count}"
        )

        per_test_lines: list[str] = []
        for r in results:
            r_icon = "✅" if r.passed else "❌"
            r_verdict = "PASS" if r.passed else "FAIL"
            capability = (getattr(r, "capability", "") or "Behavioural E2E verification").strip()
            line = (
                f"{r_icon} *{r.id}* [{r_verdict}] ({r.duration_seconds:.2f}s) — {r.name}\n"
                f"   • _Skill / Capability:_ {capability}"
            )
            if not r.passed and getattr(r, "details", ""):
                line += f"\n   • _Details:_ {r.details}"
            per_test_lines.append(line)

        reply_chunks = self._chunk_summary_lines(per_test_lines, max_chars=2800)
        summary_entry: dict[str, Any] = {
            "run_prefix": run_prefix,
            "header_text": header_text,
            "thread_replies": reply_chunks,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "total_count": total_count,
            "thread_ts": None,
        }
        self.bus.run_summaries.append(summary_entry)

        if not self.live_mode or not self.live_channel_id:
            return None

        summary_ts: str | None = None
        try:
            res = await self.raw_slack_client.chat_postMessage(
                channel=self.live_channel_id,
                text=header_text,
                username="E2E Test Runner",
                icon_url=resolve_avatar_url("static/image_99.png"),
            )
            if res and res.get("ok") and res.get("ts"):
                summary_ts = str(res.get("ts"))
                summary_entry["thread_ts"] = summary_ts
                for chunk in reply_chunks:
                    await self.raw_slack_client.chat_postMessage(
                        channel=self.live_channel_id,
                        thread_ts=summary_ts,
                        text=chunk,
                        username="E2E Test Runner",
                        icon_url=resolve_avatar_url("static/image_99.png"),
                    )
            else:
                logger.warning(f"[LIVE SLACK] Could not post run summary root message: {res}")
        except Exception as e:  # noqa: BLE001 - Live Slack run summary boundary
            logger.error(f"[LIVE SLACK] Error posting final E2E run summary thread: {e}")
        return summary_ts

    # Outbound Slack API Handlers (Dual-Mode: records locally and optionally dispatches live)
    async def mock_post_agent_response(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        username: str | None = None,
        icon_url: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        has_live_token = bool(self.bot_token and self.bot_token != "xoxb-your-bot-user-oauth-token")
        if _SUPPRESS_SPY_POST_RECORD.get():
            if self.live_mode and has_live_token:
                _sc.reset_post_agent_response()
                try:
                    return await _REAL_SLACK_POST_AGENT(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        text=text,
                        username=username or "Agent",
                        icon_url=icon_url,
                        blocks=blocks,
                        **kwargs,
                    )
                finally:
                    _sc.configure_post_agent_response(self.mock_post_agent_response)
            return {"ok": True, "ts": self.next_ts()}

        real_result = None
        if self.live_mode and has_live_token:
            tok = _SUPPRESS_SPY_POST_RECORD.set(True)
            try:
                _sc.reset_post_agent_response()
                real_result = await _REAL_SLACK_POST_AGENT(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                    username=username or "Agent",
                    icon_url=icon_url,
                    blocks=blocks,
                    **kwargs,
                )
            except Exception as e:  # noqa: BLE001 - Live Slack dispatch error boundary
                logger.error(f"[LIVE SLACK] Failed to post agent response: {e}")
            finally:
                _sc.configure_post_agent_response(self.mock_post_agent_response)
                _SUPPRESS_SPY_POST_RECORD.reset(tok)

        ts = (
            str(real_result.get("ts"))
            if real_result and real_result.get("ok") and real_result.get("ts")
            else None
        ) or self.next_ts()
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
        if (
            self.live_mode
            and self.bot_token
            and self.bot_token != "xoxb-your-bot-user-oauth-token"
        ):
            try:
                real_result = await _REAL_SLACK_UPDATE(
                    channel_id=channel_id, ts=ts, text=text
                )
            except Exception as e:  # noqa: BLE001 - Live Slack message update boundary
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
        if (
            self.live_mode
            and self.bot_token
            and self.bot_token != "xoxb-your-bot-user-oauth-token"
        ):
            tok = _SUPPRESS_SPY_POST_RECORD.set(True)
            try:
                _sc.reset_post_system_message()
                real_result = await _REAL_SLACK_POST_SYSTEM(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                    username=username,
                    icon_url=icon_url,
                )
            except Exception as e:  # noqa: BLE001 - Live Slack system message post boundary
                logger.error(f"[LIVE SLACK] Failed to post system message: {e}")
            finally:
                _sc.configure_post_system_message(self.mock_post_system_message)
                _SUPPRESS_SPY_POST_RECORD.reset(tok)

        ts = (
            str(real_result.get("ts"))
            if real_result and real_result.get("ok") and real_result.get("ts")
            else None
        ) or self.next_ts()
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

    async def mock_post_thread_note(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        pin: bool = True,
        username: str = "Live Discussion Scribe",
        icon_emoji: str = ":memo:",
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        real_result = None
        target_channel = self.get_channel(channel_id)
        target_thread = thread_ts or self.current_thread_ts

        if (
            self.live_mode
            and SLACK_BOT_TOKEN
            and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
        ):
            try:
                real_result = await _REAL_SLACK_POST_NOTE(
                    channel_id=target_channel,
                    thread_ts=target_thread,
                    text=text,
                    pin=False,
                    username=username,
                    icon_emoji=icon_emoji,
                    blocks=blocks,
                )
            except Exception as e:  # noqa: BLE001 - Live Slack note post boundary
                logger.error(f"[LIVE SLACK] Failed to post thread note: {e}")

        ts = (
            str(real_result.get("ts"))
            if real_result and real_result.get("ok") and real_result.get("ts")
            else None
        ) or self.next_ts()
        entry = {
            "ok": True,
            "channel_id": target_channel,
            "thread_ts": target_thread,
            "ts": ts,
            "text": text,
            "username": username,
            "blocks": blocks,
            "is_scribe_note": True,
            "raw_slack_response": real_result,
        }
        self.bus.thread_notes.append(entry)
        self.bus.posted_messages.append(entry)
        return {"ok": True, "ts": ts}

    async def mock_update_thread_note(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        target_channel = self.get_channel(channel_id)

        if (
            self.live_mode
            and SLACK_BOT_TOKEN
            and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
        ):
            try:
                await _REAL_SLACK_UPDATE_NOTE(
                    channel_id=target_channel,
                    message_ts=message_ts,
                    text=text,
                    blocks=blocks,
                )
            except Exception as e:  # noqa: BLE001 - Live Slack note update boundary
                logger.error(f"[LIVE SLACK] Failed to update thread note: {e}")

        for note in self.bus.thread_notes:
            if note.get("ts") == message_ts:
                note["text"] = text
                note["blocks"] = blocks
        for msg in self.bus.posted_messages:
            if msg.get("ts") == message_ts:
                msg["text"] = text
                msg["blocks"] = blocks
        return {"ok": True, "ts": message_ts}

    async def mock_post_ephemeral(
        self,
        channel_id: str,
        user_id: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        real_result = None
        target_thread = thread_ts or self.current_thread_ts
        if (
            self.live_mode
            and SLACK_BOT_TOKEN
            and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
        ):
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
            except Exception as e:  # noqa: BLE001 - Live Slack ephemeral post fallback
                logger.warning(
                    f"[LIVE SLACK] Ephemeral post failed, posting as thread note: {e}"
                )
                try:
                    await _REAL_SLACK_POST_SYSTEM(
                        channel_id=channel_id,
                        thread_ts=target_thread or "",
                        text=f"🔒 _[Ephemeral Notification for {user_id}]:_ {text}",
                    )
                except Exception as fallback_err:  # noqa: BLE001 - Non-critical ephemeral fallback logging
                    logger.debug(
                        f"[LIVE SLACK] Ephemeral fallback thread post failed: {fallback_err}"
                    )

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
        if (
            self.live_mode
            and SLACK_BOT_TOKEN
            and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
        ):
            try:
                real_result = await _REAL_SLACK_UPLOAD_FILE(
                    channel_id=channel_id,
                    content=content,
                    filename=filename,
                    title=title,
                    initial_comment=initial_comment,
                    thread_ts=target_thread,
                )
            except Exception as e:  # noqa: BLE001 - Live Slack file upload boundary
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
        max_messages: int = 500,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        # 1. Thread history store override (e.g. TC-08)
        if (channel_id, thread_ts) in self.bus.thread_history_store:
            return self.bus.thread_history_store[(channel_id, thread_ts)]

        # 2. Live Slack retrieval if live mode is active (authentic call directly to Slack)
        if (
            self.live_mode
            and SLACK_BOT_TOKEN
            and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
        ):
            _sc.reset_fetch_thread_history()
            try:
                return await _REAL_SLACK_FETCH_HISTORY(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    bot_user_id=bot_user_id,
                )
            finally:
                _sc.configure_fetch_thread_history(self.mock_fetch_thread_history)

        # 3. Offline simulation mode only: reconstruct history from in-memory bus messages
        history: list[dict[str, str]] = []
        for msg in self.bus.posted_messages:
            msg_ch = msg.get("channel_id")
            msg_th = msg.get("thread_ts")
            if msg_ch == channel_id and (
                not thread_ts or msg_th == thread_ts or msg.get("ts") == thread_ts
            ):
                if (
                    msg.get("is_scribe_note")
                    or msg.get("is_usage_card")
                    or msg.get("is_hitl_card")
                    or msg.get("username")
                    in (
                        "Live Discussion Scribe",
                        "Discussion Conclusion",
                        "Izvor Usage Analyst",
                        "Panel System",
                        "Orchestrator",
                        "E2E Test Runner",
                    )
                    or str(msg.get("text", ""))
                    .strip()
                    .startswith(("⚠️", "⚙️", "🔒", "🗑️", "🧪", "⛔"))
                ):
                    continue
                role = msg.get("role", "assistant")
                uname = msg.get("username", "Agent")
                text = msg.get("text", "")
                clean_text = re.sub(
                    r"^👤\s*\*?Follow-up Prompt:\*?\s*\n?>?\s*", "", text
                )
                if role == "user":
                    history.append({"role": "user", "content": clean_text})
                else:
                    history.append(
                        {"role": "assistant", "content": f"[{uname}]: {clean_text}"}
                    )

        return history

    async def wait_for_scribe_note(
        self, channel_id: str, thread_ts: str, timeout: float = 12.0
    ) -> str | None:
        """Waits for the background scribe worker task to finish and returns the note text."""
        from agents.thread_notes_agent import _RUNNING_TASKS, get_thread_note_content

        deadline = time.time() + timeout
        thread_key = (channel_id, thread_ts)

        while time.time() < deadline:
            task = _RUNNING_TASKS.get(thread_key)
            if task and not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=max(0.5, deadline - time.time())
                    )
                except (TimeoutError, asyncio.CancelledError):
                    logger.debug(
                        "[LIVE SLACK] Timed out waiting for background task during note check"
                    )
                except Exception as wait_err:  # noqa: BLE001 - Background task wait error boundary
                    logger.warning(
                        f"[LIVE SLACK] Error waiting for note task: {wait_err}"
                    )
            content = get_thread_note_content(channel_id, thread_ts)
            if content:
                return content
            bus_notes = [
                m
                for m in self.bus.thread_notes
                if m.get("channel_id") == channel_id
                and (not thread_ts or m.get("thread_ts") == thread_ts)
            ]
            if bus_notes:
                return bus_notes[-1].get("text", "")
            await asyncio.sleep(0.1)

        return get_thread_note_content(channel_id, thread_ts)

    async def mock_fetch_thread_files(
        self,
        channel_id: str,
        thread_ts: str,
        **kwargs: Any,
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
        files.append(
            {
                "id": f"F_{file_name}",
                "name": file_name,
                "size": size or len(content.encode("utf-8")),
                "mimetype": "text/plain",
                "url_private_download": f"https://slack.local/files/{file_name}",
                "content": content,
                "is_resource_file": False,
            }
        )

    # Inbound Slack Actions Simulator
    async def send_event(
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
                "thread_ts": actual_thread_ts,
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
        return await self.async_client.post(
            "/slack/events", content=body_bytes, headers=headers
        )

    async def send_mention(
        self,
        text: str,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str | None = None,
        user_id: str = "U_USER_TESTER",
        wait_for_task: bool = True,
    ) -> Any:
        """Simulates an app_mention event directed at the bot, integrating the prompt into the root card or follow-up."""
        target_channel = self.get_channel(channel_id)

        if self.live_mode and self.live_channel_id:
            if not self.current_thread_ts:
                # First prompt of this scenario: create the top-level conversation card with prompt embedded
                await self.start_live_scenario(prompt_text=text)
                target_thread = self.current_thread_ts
            else:
                # Follow-up turn in an existing thread (e.g. TC-10 revocation)
                target_thread = self.current_thread_ts
                try:
                    await _raw_slack_async_client.chat_postMessage(
                        channel=target_channel,
                        thread_ts=target_thread,
                        text=f"👤 *Follow-up Prompt:*\n> {text}",
                        username="Tester",
                        icon_url=resolve_avatar_url("static/image_99.png"),
                    )
                except Exception as e:  # noqa: BLE001 - Live Slack follow-up post boundary
                    logger.warning(
                        f"[LIVE SLACK] Failed to post follow-up prompt to thread: {e}"
                    )
        else:
            ts = self.next_ts()
            target_thread = self.get_thread_ts(thread_ts) or thread_ts or ts
            self.current_thread_ts = target_thread

        mention_text = f"<@{self.bot_user_id}> {text}"
        self.bus.posted_messages.append(
            {
                "channel_id": target_channel,
                "thread_ts": target_thread or "",
                "ts": target_thread
                if (not self.live_mode and not thread_ts)
                else self.next_ts(),
                "text": text,
                "role": "user",
                "username": "Tester",
            }
        )
        res = await self.send_event(
            event_type="app_mention",
            text=mention_text,
            channel_id=target_channel,
            thread_ts=target_thread,
            user_id=user_id,
        )
        if target_thread:
            self._dispatched_threads.add((target_channel, target_thread))
        if wait_for_task:
            from orchestration.thread_state import thread_state_manager

            task = thread_state_manager.get_active_task(
                target_channel, target_thread or ""
            )
            if task and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as task_err:  # noqa: BLE001 - Test client absorbs background task exceptions
                    logger.debug(
                        f"[LIVE SLACK] Dispatched background task finished with error: {task_err}"
                    )
        return res

    async def send_block_action(
        self,
        action_id: str,
        value: str | None = None,
        selected_option_value: str | None = None,
        channel_id: str = "C_TEST_CHANNEL",
        thread_ts: str = "1700000000.0001",
        user_id: str = TEST_USER_ID,
        view_state: dict[str, Any] | None = None,
        message_ts: str | None = None,
        wait_for_task: bool = False,
    ) -> Any:
        """Simulates an interactive Block Kit button click or toggle via POST /slack/actions."""
        target_channel = self.get_channel(channel_id)

        if self.live_mode and self.live_channel_id:
            if not self.current_thread_ts:
                await self.start_live_scenario(
                    prompt_text=f"Action: `{action_id}` (value: `{value}`)"
                )
            target_thread = self.current_thread_ts
        else:
            target_thread = self.get_thread_ts(thread_ts) or thread_ts

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
            "message": {"ts": message_ts or self.next_ts(), "thread_ts": target_thread},
            "actions": [action_dict],
            "view": {
                "user": {"id": user_id},
                "state": {"values": view_state or {}},
            },
        }
        form_body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode(
            "utf-8"
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            **self.sign_body(form_body),
        }
        res = await self.async_client.post(
            "/slack/actions", content=form_body, headers=headers
        )
        if target_thread:
            self._dispatched_threads.add((target_channel, target_thread))
        if wait_for_task and target_thread:
            await asyncio.sleep(0.05)
            await self.await_active_task(target_channel, target_thread)
        return res
