import re
import json
import logging
from typing import Tuple, Dict, Any, List
import litellm

from config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from slack_client import post_agent_response, fetch_thread_history
from mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True

def parse_agent_and_prompt(text: str) -> Tuple[str, str]:
    """
    Parses Slack message text for pseudo-agent tags or @mentions.
    Supported patterns:
    - [Gemini_Visionary] prompt...
    - @Gemini_Visionary prompt...
    - @CaiLogics Hub [Mistral_TechLead] prompt...
    - @CaiLogics Visionary prompt...
    - Plain text -> defaults to Gemini_Architect

    Returns (agent_key, cleaned_prompt)
    """
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

    # Search for explicit bracketed tags like [Gemini_Visionary] or [Mistral_TechLead]
    tag_match = re.search(r"\[([a-zA-Z0-9_]+)\]", cleaned_text)
    if tag_match:
        tag = tag_match.group(1)
        for key in AGENTS_CONFIG.keys():
            if tag.lower() == key.lower():
                prompt = re.sub(r"\[" + re.escape(tag) + r"\]", "", cleaned_text).strip()
                return key, prompt

    # Search for direct @Name or name mentions in text
    for key, cfg in AGENTS_CONFIG.items():
        # Check against dictionary key (e.g. Gemini_Visionary or Mistral_TechLead)
        if re.search(r"@?" + re.escape(key), cleaned_text, re.IGNORECASE):
            prompt = re.sub(r"@?" + re.escape(key), "", cleaned_text, flags=re.IGNORECASE).strip()
            return key, prompt

        # Check against configured Slack persona username (e.g. "CaiLogics Visionary")
        persona_name = cfg.get("username", "")
        if persona_name and re.search(r"@?" + re.escape(persona_name), cleaned_text, re.IGNORECASE):
            prompt = re.sub(r"@?" + re.escape(persona_name), "", cleaned_text, flags=re.IGNORECASE).strip()
            return key, prompt

    # Default fallback agent if no tag or name matched
    return DEFAULT_AGENT_KEY, cleaned_text

async def process_and_respond(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str = None
) -> Dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    """
    agent_key, prompt = parse_agent_and_prompt(raw_text)
    agent_cfg = AGENTS_CONFIG.get(agent_key, AGENTS_CONFIG[DEFAULT_AGENT_KEY])

    logger.info(f"Routing request to Agent: {agent_key} ({agent_cfg['model']}, temp={agent_cfg['temperature']})")

    # 1. Fetch thread history context
    history = await fetch_thread_history(channel_id, thread_ts, bot_user_id=bot_user_id)

    # 2. Build message sequence for LiteLLM
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": agent_cfg["system_prompt"]}
    ]

    if history:
        messages.extend(history)
        # Avoid duplicating current prompt if it's already the last item in thread history
        if not (history and history[-1]["role"] == "user" and prompt in history[-1]["content"]):
            messages.append({"role": "user", "content": prompt})
    else:
        messages.append({"role": "user", "content": prompt})

    try:
        # 3. Call LiteLLM model with MCP tools
        response = await litellm.acompletion(
            model=agent_cfg["model"],
            messages=messages,
            temperature=agent_cfg["temperature"],
            tools=MCP_TOOLS_SCHEMAS,
            tool_choice="auto"
        )

        response_message = response.choices[0].message

        # 4. Handle Tool Calls if model requested MCP tool execution
        tool_calls = getattr(response_message, "tool_calls", None)
        if tool_calls:
            # Append model's assistant message with tool calls
            messages.append(response_message.model_dump())

            for tool_call in tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str) else tool_call.function.arguments
                tool_result = execute_mcp_tool(fn_name, fn_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": fn_name,
                    "content": tool_result
                })

            # Follow-up completion after tool results
            follow_up = await litellm.acompletion(
                model=agent_cfg["model"],
                messages=messages,
                temperature=agent_cfg["temperature"]
            )
            final_text = follow_up.choices[0].message.content
        else:
            final_text = response_message.content

        if not final_text:
            final_text = "(No text response generated)"

        # 5. Post response back to Slack thread with dynamic persona
        slack_res = await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=final_text,
            username=agent_cfg["username"],
            icon_emoji=agent_cfg["icon_emoji"]
        )
        return slack_res

    except Exception as e:
        logger.error(f"Error during agent completion or post: {e}", exc_info=True)
        error_msg = f"⚠️ *Error processing request with {agent_cfg['username']}*: {str(e)}"
        await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=error_msg,
            username=agent_cfg["username"],
            icon_emoji=agent_cfg["icon_emoji"]
        )
        return {"ok": False, "error": str(e)}
