import json
import logging
import re

import litellm
from starlette.concurrency import run_in_threadpool

from config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from slack_client import fetch_thread_history, post_agent_response

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True

# Track agents who haven't spoken yet per thread (mapped by (channel_id, thread_ts))
REMAINING_AGENTS_MAP = {}

# Track threads that are running a custom tagged agent sequence
CUSTOM_FLOW_THREADS = set()


def get_initial_agents() -> list[str]:
    """Returns the list of all optional agents that should speak in the discussion flow."""
    return [
        key
        for key in AGENTS_CONFIG
        if key not in ("OpAiTerra_Architect", "Gemini_Visionary")
    ]


def extract_tagged_agents(text: str) -> list[str]:
    """
    Extracts all valid agent keys mentioned/tagged in the text,
    ordered by their appearance in the text.
    """
    matches = []
    # Clean text to remove bot mentions
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)

    # 1. Bracketed tags
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned):
        tag = match.group(1).lower()
        for key in AGENTS_CONFIG:
            if tag == key.lower():
                matches.append((match.start(), key))
                break

    # 2. Key or username mentions
    for key, cfg in AGENTS_CONFIG.items():
        for match in re.finditer(r"@?" + re.escape(key), cleaned, re.IGNORECASE):
            matches.append((match.start(), key))
        username = cfg.get("username", "")
        if username:
            for match in re.finditer(
                r"@?" + re.escape(username), cleaned, re.IGNORECASE
            ):
                matches.append((match.start(), key))

    # Sort matches by position in text to preserve order
    matches.sort(key=lambda x: x[0])

    # Deduplicate while preserving order
    unique_keys = []
    for pos, key in matches:
        if key not in unique_keys:
            unique_keys.append(key)

    return unique_keys


def parse_agent_and_prompt(
    text: str, channel_id: str, thread_ts: str
) -> tuple[str, str, bool]:
    """
    Parses Slack message text for pseudo-agent tags or @mentions.
    Supported patterns:
    - [Gemini_Visionary] prompt...
    - @Gemini_Visionary prompt...
    - @CaiLogics Hub [Mistral_TechLead] prompt...
    - @CaiLogics Visionary prompt...
    - Plain text -> defaults to Gemini_Visionary
    Note: If no agent is specified, the default flow is to start with Gemini_Visionary, go through all others, and end with OpAiTerra_Architect. Each agent will nominate the next agent to speak.
    Returns (agent_key, cleaned_prompt, was_explicit_target)
    """
    global REMAINING_AGENTS_MAP
    thread_key = (channel_id, thread_ts)
    if thread_key not in REMAINING_AGENTS_MAP:
        logger.info(
            f"[ROUTER] INITIALIZING new remaining agents list for thread_key: {thread_key}"
        )
        REMAINING_AGENTS_MAP[thread_key] = get_initial_agents()

    remaining = REMAINING_AGENTS_MAP[thread_key]
    logger.info(
        f"[ROUTER] Current remaining agents for thread_key {thread_key}: {remaining}"
    )
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

    # Search for explicit bracketed tags like [Gemini_Visionary] or [Mistral_TechLead]
    tag_matches = []
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned_text):
        tag = match.group(1)
        for key in AGENTS_CONFIG:
            if tag.lower() == key.lower():
                tag_matches.append((match, key, tag))
                break

    if tag_matches:
        # Prioritize matching an agent that is currently in remaining
        matched_item = None
        for match, key, tag in tag_matches:
            if key in remaining:
                matched_item = (match, key, tag)
                break

        # If none matched remaining (e.g. OpAiTerra_Architect or an agent already spoken):
        if not matched_item:
            for match, key, tag in tag_matches:
                if key == "OpAiTerra_Architect" and not remaining:
                    matched_item = (match, key, tag)
                    break
            if not matched_item:
                matched_item = tag_matches[0]

        match, matched_key, tag = matched_item
        prompt = re.sub(r"\[" + re.escape(tag) + r"\]", "", cleaned_text).strip()
        if matched_key in remaining:
            remaining.remove(matched_key)
        return matched_key, prompt, True

    # Search for direct @Name or name mentions in text
    for key, cfg in AGENTS_CONFIG.items():
        # Check against dictionary key (e.g. Gemini_Visionary or Mistral_TechLead)
        if re.search(r"@?" + re.escape(key), cleaned_text, re.IGNORECASE):
            prompt = re.sub(
                r"@?" + re.escape(key), "", cleaned_text, flags=re.IGNORECASE
            ).strip()
            if key in remaining:
                remaining.remove(key)
            return key, prompt, True

        # Check against configured Slack persona username (e.g. "CaiLogics Visionary")
        persona_name = cfg.get("username", "")
        if persona_name and re.search(
            r"@?" + re.escape(persona_name), cleaned_text, re.IGNORECASE
        ):
            prompt = re.sub(
                r"@?" + re.escape(persona_name), "", cleaned_text, flags=re.IGNORECASE
            ).strip()
            if key in remaining:
                remaining.remove(key)
            return key, prompt, True

    # Default fallback agent if no tag or name matched
    if DEFAULT_AGENT_KEY in remaining:
        remaining.remove(DEFAULT_AGENT_KEY)
    return DEFAULT_AGENT_KEY, cleaned_text, False


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
) -> str:
    """
    Ensures that the agent's response nominates a valid agent:
    1. If force_termination or current agent is OpAiTerra_Architect, no nomination is required.
    2. If remaining agents exist:
       - Preserves the agent's nomination if it chose an agent that is still in `remaining`.
       - If the agent prematurely nominated OpAiTerra_Architect or an agent that already spoke,
         programmatically replaces the nomination tag with the first remaining agent (remaining[0]).
       - If no nomination tag is present, appends [remaining[0]].
    3. If remaining list is empty and current agent is not OpAiTerra_Architect:
       - Ensures [OpAiTerra_Architect] is nominated for the final summary.
    """
    if force_termination or current_agent_key == "OpAiTerra_Architect":
        # Strip any bracketed agent tags so Slack webhook won't trigger unexpected bot loops
        for key in AGENTS_CONFIG:
            text = re.sub(r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE)
        return text

    # Find all bracketed agent tags matching known AGENTS_CONFIG keys
    agent_tag_matches = []
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", text):
        tag_name = match.group(1)
        for key in AGENTS_CONFIG:
            if tag_name.lower() == key.lower():
                agent_tag_matches.append((match, key))
                break

    if remaining:
        # Check if any detected tag is actually in remaining
        valid_nomination = None
        for match, key in agent_tag_matches:
            if key in remaining:
                valid_nomination = key
                break

        if valid_nomination:
            logger.info(
                f"[ROUTER] Agent {current_agent_key} chose valid remaining agent [{valid_nomination}]."
            )
            return text

        fallback_agent = remaining[0]
        if agent_tag_matches:
            last_match, last_key = agent_tag_matches[-1]
            logger.warning(
                f"[ROUTER] Agent {current_agent_key} nominated '{last_key}', but {len(remaining)} agent(s) have not spoken yet: {remaining}. "
                f"Programmatically correcting nomination tag to [{fallback_agent}]."
            )
            start, end = last_match.span()
            return text[:start] + f"[{fallback_agent}]" + text[end:]
        else:
            logger.warning(
                f"[ROUTER] Agent {current_agent_key} omitted nomination tag while {len(remaining)} agent(s) remain: {remaining}. "
                f"Programmatically appending [{fallback_agent}]."
            )
            return f"{text.strip()}\n\n[{fallback_agent}]"
    else:
        # remaining list is empty -> next must be OpAiTerra_Architect
        if agent_tag_matches:
            last_match, last_key = agent_tag_matches[-1]
            if last_key == "OpAiTerra_Architect":
                return text
            logger.info(
                f"[ROUTER] All intermediate agents have spoken. Correcting tag '[{last_key}]' to [OpAiTerra_Architect]."
            )
            start, end = last_match.span()
            return text[:start] + "[OpAiTerra_Architect]" + text[end:]
        else:
            logger.info(
                f"[ROUTER] All intermediate agents have spoken. Appending [OpAiTerra_Architect] for final summary."
            )
            return f"{text.strip()}\n\n[OpAiTerra_Architect]"


def markdown_to_slack(text: str) -> str:
    # Bold: **tekst** -> *tekst*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    # Headings: ### Naslov -> *Naslov*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Links: [label](url) -> <url|label>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"<\2|\1>", text)
    return text


async def process_and_respond(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    is_bot_trigger: bool = False,
) -> dict[str, any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    """
    import time

    start_time = time.time()
    logger.info(
        f"\n[{start_time}] >>> START process_and_respond for thread_ts: {thread_ts} <<<"
    )

    agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
        raw_text, channel_id, thread_ts
    )

    thread_key = (channel_id, thread_ts)

    # Adjust remaining agents list if user input explicitly tagged several agents
    if not is_bot_trigger:
        tagged = extract_tagged_agents(raw_text)
        if tagged:
            agent_key = tagged[0]
            remaining_tagged = [a for a in tagged if a != agent_key]
            REMAINING_AGENTS_MAP[thread_key] = remaining_tagged
            CUSTOM_FLOW_THREADS.add(thread_key)
            was_explicit_target = True
        else:
            CUSTOM_FLOW_THREADS.discard(thread_key)

    agent_cfg = AGENTS_CONFIG.get(agent_key, AGENTS_CONFIG[DEFAULT_AGENT_KEY])

    logger.info(
        f"[ROUTER] Parsed Agent: {agent_key} ({agent_cfg['model']}) | Explicit target: {was_explicit_target} | Bot trigger: {is_bot_trigger}"
    )

    # 1. Fetch thread history context
    history = await fetch_thread_history(channel_id, thread_ts, bot_user_id=bot_user_id)
    logger.info(f"[ROUTER] Fetched {len(history)} messages from thread history.")

    # 2. Build message sequence for LiteLLM
    system_prompt = agent_cfg["system_prompt"]

    remaining = REMAINING_AGENTS_MAP.get(thread_key, get_initial_agents())
    is_custom_flow = thread_key in CUSTOM_FLOW_THREADS

    # Determine if we should force termination (no more nominations)
    force_termination = False
    if was_explicit_target and not is_bot_trigger and not remaining:
        force_termination = True
    elif is_custom_flow and not remaining:
        force_termination = True

    if force_termination:
        logger.info(
            f"[ROUTER] Agent {agent_key} forcing termination (no remaining agents in sequence)."
        )
        system_prompt += (
            "\n\nCRITICAL CONSTRAINT: You are the last agent requested to speak in this turn. "
            "Do NOT nominate any other agent to speak next. Do NOT include any bracketed agent key (like [Mistral_Innovator] "
            "or [OpAiTerra_Architect]) at the end of your response. Simply answer the question/summarize and finish your response."
        )
    else:
        # Standard nomination loop
        if remaining:
            system_prompt = f"{system_prompt}\n{', '.join(remaining)}"
    logger.info(
        f"[ROUTER] Final system prompt with available agents added {system_prompt} ."
    )

    messages: list[dict[str, any]] = [{"role": "system", "content": system_prompt}]

    if history:
        messages.extend(history)
        # Avoid duplicating current prompt if it's already the last item in thread history
        if not (history[-1]["role"] == "user" and prompt in history[-1]["content"]):
            messages.append({"role": "user", "content": prompt})
    else:
        messages.append({"role": "user", "content": prompt})

    try:
        total_prompt_tokens = 0
        total_completion_tokens = 0

        def add_usage(resp):
            nonlocal total_prompt_tokens, total_completion_tokens
            if not resp:
                return
            usage = getattr(resp, "usage", None)
            if not usage:
                return
            if isinstance(usage, dict):
                total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
                total_completion_tokens += usage.get("completion_tokens", 0) or 0
            else:
                total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0

        # 3. Call LiteLLM model with MCP tools in a threadpool to prevent freezing the event loop
        logger.info(f"[LLM] Dispatching litellm.completion for {agent_key}...")
        llm_start = time.time()
        response = await run_in_threadpool(
            litellm.completion,
            model=agent_cfg["model"],
            messages=messages,
            # temperature=agent_cfg["temperature"],
            tools=MCP_TOOLS_SCHEMAS,
            tool_choice="auto",
        )
        add_usage(response)
        llm_duration = time.time() - llm_start
        logger.info(
            f"[LLM] Received response for {agent_key} in {llm_duration:.2f} seconds."
        )

        response_message = response.choices[0].message

        # 4. Handle Tool Calls if model requested MCP tool execution
        tool_calls = getattr(response_message, "tool_calls", None)
        if tool_calls:
            # Append model's assistant message with tool calls
            messages.append(response_message.model_dump())

            for tool_call in tool_calls:
                fn_name = tool_call.function.name
                fn_args = (
                    json.loads(tool_call.function.arguments)
                    if isinstance(tool_call.function.arguments, str)
                    else tool_call.function.arguments
                )
                tool_result = execute_mcp_tool(fn_name, fn_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": fn_name,
                        "content": tool_result,
                    }
                )

            # Follow-up completion after tool results in a threadpool
            follow_up = await run_in_threadpool(
                litellm.completion,
                model=agent_cfg["model"],
                messages=messages,
                # temperature=agent_cfg["temperature"],
            )
            add_usage(follow_up)
            final_text = follow_up.choices[0].message.content
        else:
            final_text = response_message.content

        if not final_text:
            if force_termination:
                final_text = "(No text response generated)"
            else:
                next_tag = (
                    remaining[0]
                    if remaining
                    else "OpAiTerra_Architect"
                )
                final_text = f"(No text response generated) [{next_tag}] next"
            logger.warning(f"[ROUTER] final_text was empty for {agent_key}!")
        else:
            final_text = markdown_to_slack(final_text)
            final_text = enforce_valid_nomination(
                final_text, agent_key, remaining, force_termination
            )

        # Append token usage to the end of the message text
        final_text = (
            f"{final_text.strip()}\n\n[{total_prompt_tokens}/{total_completion_tokens}]"
        )

        # Log the specific generated tag for the next agent
        generated_tags = re.findall(r"\[([a-zA-Z0-9_]+)\]", final_text)
        logger.info(
            f"[ROUTER] Agent {agent_key} generated tag(s): {generated_tags}. Full text length: {len(final_text)}"
        )

        # Prevent Slack WebSocket client saturation by adding a 3-second pacing delay.
        # This keeps the real-time stream active and makes the conversation pace feel natural.
        import asyncio

        await asyncio.sleep(3.0)

        # 5. Post response back to Slack thread with dynamic persona
        logger.info(
            f"[SLACK] Posting response to channel {channel_id}, thread_ts {thread_ts} as {agent_cfg['username']}"
        )
        slack_res = await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=final_text,
            username=agent_cfg["username"],
            icon_url=agent_cfg.get("icon_url"),
        )
        posted_ts = slack_res.get("ts")
        logger.info(f"[SLACK] Successfully posted message! Received ts: {posted_ts}")

        # Reset remaining agents/custom flows for this thread when finishing
        if force_termination and thread_key in CUSTOM_FLOW_THREADS:
            logger.info(
                f"[ROUTER] Custom sequence flow finished! Deleting state for thread {thread_key}"
            )
            CUSTOM_FLOW_THREADS.discard(thread_key)
            if thread_key in REMAINING_AGENTS_MAP:
                del REMAINING_AGENTS_MAP[thread_key]
        elif (
            agent_key == "OpAiTerra_Architect" and thread_key in REMAINING_AGENTS_MAP
        ):
            logger.info(
                f"[ROUTER] OpAiTerra_Architect finished! Deleting REMAINING_AGENTS_MAP for thread {thread_key}"
            )
            del REMAINING_AGENTS_MAP[thread_key]

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {thread_ts} (Total: {time.time() - start_time:.2f}s) <<<\n"
        )
        return slack_res

    except Exception as e:
        logger.exception(f"[ERROR] Exception during agent completion or post: {e}")
        error_msg = f"⚠️ *Error processing request with {agent_cfg['username']}*: {e!s}"
        await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=error_msg,
            username="System Error",
        )
        return {"ok": False, "error": str(e)}
