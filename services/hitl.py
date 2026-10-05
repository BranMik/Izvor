import logging
from typing import Any
from services.slack_client import slack_async_client, slack_rate_limiter

logger = logging.getLogger(__name__)

# Stores approved tools: (channel_id, thread_ts, fn_name)
APPROVED_TOOLS: set[tuple[str, str, str]] = set()

def consume_tool_approval(channel_id: str, thread_ts: str, fn_name: str) -> bool:
    key = (channel_id, thread_ts, fn_name)
    if key in APPROVED_TOOLS:
        APPROVED_TOOLS.remove(key)
        return True
    return False

def mark_tool_approved(channel_id: str, thread_ts: str, fn_name: str) -> None:
    APPROVED_TOOLS.add((channel_id, thread_ts, fn_name))

async def request_human_approval(
    channel_id: str,
    thread_ts: str,
    agent_name: str,
    skill_name: str,
    fn_name: str,
    fn_args: dict[str, Any]
) -> None:
    args_str = str(fn_args)[:1000]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⚠️ *Action Required: Human-in-the-Loop Approval*\nAgent *{agent_name}* wants to execute a destructive tool.\n*Skill:* `{skill_name}`\n*Tool:* `{fn_name}`\n*Arguments:* ```{args_str}```"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "value": f"{channel_id}:{thread_ts}:{fn_name}",
                    "action_id": "hitl_approve_tool"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "value": f"{channel_id}:{thread_ts}:{fn_name}",
                    "action_id": "hitl_deny_tool"
                }
            ]
        }
    ]
    await slack_rate_limiter.acquire()
    await slack_async_client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Approval required for {fn_name}",
        blocks=blocks
    )
