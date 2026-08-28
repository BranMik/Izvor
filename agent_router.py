import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from prompts import build_agent_prompt
from llm_utils import (
    extract_anthropic_cache_usage,
    extract_cached_tokens,
    is_anthropic_model,
    is_openai_model,
    log_anthropic_cache_usage,
    log_llm_cache_usage,
    log_openai_cached_tokens,
)
from mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from slack_client import (
    download_slack_file,
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
)

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True

# Pacing and retry configuration
LLM_MAX_RETRIES = 3
LLM_RETRY_BACKOFF_BASE = 0.01 if "pytest" in sys.modules else 1.5
SLACK_POST_PAUSE_SECONDS = 0.01 if "pytest" in sys.modules else 2.0

# Path to persistent settings file
SETTINGS_FILE = Path(__file__).parent / "agent_settings.json"

# Track agents who haven't spoken yet per thread (mapped by (channel_id, thread_ts))
REMAINING_AGENTS_MAP = {}

# Track agents deferred due to rate limits waiting for retry at the end of discussion
# Map: (channel_id, thread_ts) -> list[str] (agent keys)
RATE_LIMIT_RETRY_QUEUE: dict[tuple[str, str], list[str]] = {}

# Track agents that have already been retried once for this thread to prevent infinite retry loops
# Map: (channel_id, thread_ts) -> set[str] (agent keys)
RATE_LIMIT_ALREADY_RETRIED: dict[tuple[str, str], set[str]] = {}

# Track running asyncio Task for each active thread discussion
# Map: (channel_id, thread_ts) -> asyncio.Task
ACTIVE_THREAD_TASKS: dict[tuple[str, str], asyncio.Task] = {}

# Track discussion generation counter per thread to invalidate superseded tasks
# Map: (channel_id, thread_ts) -> int
THREAD_GENERATIONS: dict[tuple[str, str], int] = {}

# Track threads that are running a custom tagged agent sequence
CUSTOM_FLOW_THREADS = set()

# Track threads where a stop has been requested
STOP_REQUESTED_THREADS = set()

# Track conversation history limit and baseline index per thread
# Map: (channel_id, thread_ts) -> dict[str, int] (e.g. {"limit": 5, "prior_cutoff_index": 12})
THREAD_HISTORY_LIMITS: dict[tuple[str, str], dict[str, int]] = {}

HISTORY_COMMAND_PATTERN = re.compile(
    r"/history\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)


def extract_history_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the /history [x:int] command parameter if present in text and returns (history_limit, cleaned_text).
    Supports:
    - /history 5
    - /history: 5 or /history:5
    - /history=5
    - /history [5] or /history [5:int] or /history 5:int
    - /history (5)
    If no /history command is found, returns (None, text).
    """
    if not text:
        return None, ""
    match = HISTORY_COMMAND_PATTERN.search(text)
    if match:
        limit = int(match.group(1))
        cleaned = HISTORY_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned
    return None, text


def get_thread_history_limit(channel_id: str, thread_ts: str) -> dict[str, int] | None:
    """Returns the active history limit dict for this thread, or None."""
    return THREAD_HISTORY_LIMITS.get((channel_id, thread_ts))


def set_thread_history_limit(
    channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int
) -> None:
    """Sets the history limit and prior cutoff index for this thread."""
    THREAD_HISTORY_LIMITS[(channel_id, thread_ts)] = {
        "limit": limit,
        "prior_cutoff_index": prior_cutoff_index,
    }
    logger.info(
        f"[ROUTER] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
    )


def clear_thread_history_limit(channel_id: str, thread_ts: str) -> None:
    """Clears any active history limit for this thread."""
    THREAD_HISTORY_LIMITS.pop((channel_id, thread_ts), None)


MAX_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:max_words|max-words|maxwords)\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)

# Track conversation max words limit per thread
# Map: (channel_id, thread_ts) -> int
THREAD_MAX_WORDS_LIMITS: dict[tuple[str, str], int] = {}


def extract_max_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the /max_words [x:int] command parameter if present in text and returns (max_words_limit, cleaned_text).
    Supports:
    - /max_words 50
    - /max_words: 50 or /max_words:50
    - /max_words=50
    - /max_words [50] or /max_words [50:int] or /max_words 50:int
    - /max_words (50)
    If no /max_words command is found, returns (None, text).
    """
    if not text:
        return None, ""
    match = MAX_WORDS_COMMAND_PATTERN.search(text)
    if match:
        limit = max(1, int(match.group(1)))
        cleaned = MAX_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned
    return None, text


def get_thread_max_words(channel_id: str, thread_ts: str) -> int | None:
    """Returns the active max_words limit for this thread, or None."""
    return THREAD_MAX_WORDS_LIMITS.get((channel_id, thread_ts))


def set_thread_max_words(channel_id: str, thread_ts: str, max_words: int) -> None:
    """Sets the max_words limit for this thread."""
    THREAD_MAX_WORDS_LIMITS[(channel_id, thread_ts)] = max_words
    logger.info(
        f"[ROUTER] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}"
    )


def clear_thread_max_words(channel_id: str, thread_ts: str) -> None:
    """Clears any active max_words limit for this thread."""
    THREAD_MAX_WORDS_LIMITS.pop((channel_id, thread_ts), None)


# Track on-demand file context per thread discussion
# Map: (channel_id, thread_ts) -> dict (e.g. {"file_name": str, "content": str, "size": int, "truncated": bool, "authorized_agents": set[str] | None})
THREAD_ACTIVE_FILE_CONTEXT: dict[tuple[str, str], dict[str, Any]] = {}

FILE_COMMAND_PATTERN = re.compile(
    r"/(?:read[-_]?file|file|attachment)(?:\s*[:=]\s*(\S+)|\s+\[([^\]]+)\]|\s+\(([^)]+)\)|\s+([\w\-.]+\.[a-zA-Z0-9]+))?",
    re.IGNORECASE,
)

FILE_NL_PATTERNS = [
    # Croatian triggers
    re.compile(
        r"\b(?:pro[čc]itaj|pogledaj|analiziraj|provjeri|u[čc]itaj|pregledaj|obradi|otvori)\b.*?\b(?:fajl|datotek[a-z]*|privit[a-z]*|attachment[a-z]*|prilo[zž]en[a-z]*|kod\s+u\s+privitku|dokument[a-z]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:u\s+privitku|u\s+fajlu|u\s+datoteci|iz\s+fajla|iz\s+datoteke|iz\s+privitka|prilo[zž]en[a-z]*\s+(?:fajl|datotek|privit|kod|dokument)|uploadao\s+sam\s+fajl|poslao\s+sam\s+fajl|uploadan[a-z]*\s+fajl)\b",
        re.IGNORECASE,
    ),
    # English triggers
    re.compile(
        r"\b(?:read|check|analyze|analyse|inspect|review|examine|look\s+at|open)\b.*?\b(?:file|attachment|document|uploaded\s+file|code\s+in\s+attachment)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:attached\s+file|in\s+the\s+attachment|from\s+the\s+file|uploaded\s+file|in\s+the\s+uploaded\s+file)\b",
        re.IGNORECASE,
    ),
]


def extract_file_instruction(
    text: str, available_files: list[dict[str, Any]] | None = None
) -> tuple[bool, str | None, str]:
    """
    Detects if the user prompt requests reading an attached Slack file via command or natural language.
    Returns: (is_requested: bool, target_filename: str | None, cleaned_text: str)
    """
    if not text:
        return False, None, ""

    is_requested = False
    target_filename = None
    cleaned_text = text

    # 1. Check for slash command (e.g. /read-file, /file sample.py, /read_file: data.csv)
    cmd_match = FILE_COMMAND_PATTERN.search(text)
    if cmd_match:
        is_requested = True
        # Extract matched target if any of the filename groups matched
        target = next((g for g in cmd_match.groups() if g is not None), None)
        if target:
            target_filename = target.strip("[]():,; \t")
        cleaned_text = FILE_COMMAND_PATTERN.sub("", text).strip()
        cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
        return is_requested, target_filename, cleaned_text

    # 2. Check for natural language triggers
    for pattern in FILE_NL_PATTERNS:
        if pattern.search(text):
            is_requested = True
            break

    # 3. If available_files is provided, check if any uploaded file's name is explicitly mentioned in the text
    if available_files:
        for f in available_files:
            fname = f.get("name")
            if fname and re.search(
                r"\b" + re.escape(fname) + r"\b", text, re.IGNORECASE
            ):
                is_requested = True
                target_filename = fname
                break

    return is_requested, target_filename, cleaned_text


def resolve_target_file(
    available_files: list[dict[str, Any]], target_filename: str | None = None
) -> dict[str, Any] | None:
    """
    Selects the matching file from available thread files.
    If target_filename is specified, matches exact name or substring (case-insensitive).
    Otherwise returns the latest uploaded file in the thread.
    """
    if not available_files:
        return None
    if target_filename:
        # Exact match (case-insensitive)
        for f in reversed(available_files):
            if f.get("name", "").lower() == target_filename.lower():
                return f
        # Partial match
        for f in reversed(available_files):
            if target_filename.lower() in f.get("name", "").lower():
                return f
    # Default to latest uploaded file
    return available_files[-1]


def get_thread_file_context(channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    """Returns the active file context for this thread discussion, or None."""
    return THREAD_ACTIVE_FILE_CONTEXT.get((channel_id, thread_ts))


def set_thread_file_context(
    channel_id: str, thread_ts: str, file_context: dict[str, Any]
) -> None:
    """Sets the active file context for this thread discussion."""
    THREAD_ACTIVE_FILE_CONTEXT[(channel_id, thread_ts)] = file_context
    logger.info(
        f"[ROUTER] Set active file context for thread {(channel_id, thread_ts)}: file='{file_context.get('file_name')}', authorized_agents={file_context.get('authorized_agents')}"
    )


def clear_thread_file_context(channel_id: str, thread_ts: str) -> None:
    """Clears any active file context for this thread."""
    THREAD_ACTIVE_FILE_CONTEXT.pop((channel_id, thread_ts), None)


def is_discussion_active(channel_id: str, thread_ts: str) -> bool:
    """
    Checks if there is an active (ongoing, not completed) discussion in this thread.
    A discussion is active if:
    - An asyncio.Task is currently executing for this thread and not done, OR
    - Intermediate agents are remaining in REMAINING_AGENTS_MAP for this thread, OR
    - Pending rate-limit retries exist in RATE_LIMIT_RETRY_QUEUE.
    """
    thread_key = (channel_id, thread_ts)
    task = ACTIVE_THREAD_TASKS.get(thread_key)
    if task and not task.done():
        return True
    if REMAINING_AGENTS_MAP.get(thread_key):
        return True
    return bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))


def interrupt_thread_discussion(channel_id: str, thread_ts: str) -> None:
    """
    Interrupts and cancels the current ongoing discussion in the specified thread:
    1. Increments the thread generation counter so in-flight tasks become superseded and abort.
    2. Cancels any active asyncio.Task currently processing for this thread.
    3. Clears remaining agents, custom flows, retry queues, and history limits.
    4. Clears any stop requested flag for this thread so the new query can run cleanly.
    """
    thread_key = (channel_id, thread_ts)
    THREAD_GENERATIONS[thread_key] = THREAD_GENERATIONS.get(thread_key, 0) + 1
    logger.info(
        f"[ROUTER] Interrupting discussion for thread {thread_key}. New generation: {THREAD_GENERATIONS[thread_key]}"
    )

    task = ACTIVE_THREAD_TASKS.pop(thread_key, None)
    if task and not task.done() and task is not asyncio.current_task():
        logger.info(
            f"[ROUTER] Cancelling active background task for thread {thread_key}"
        )
        task.cancel()

    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
    THREAD_HISTORY_LIMITS.pop(thread_key, None)
    THREAD_ACTIVE_FILE_CONTEXT.pop(thread_key, None)
    CUSTOM_FLOW_THREADS.discard(thread_key)
    STOP_REQUESTED_THREADS.discard(thread_key)


def classify_agent_error(e: Exception) -> str:
    """
    Classifies an agent execution exception into one of:
    - 'api_key_not_found': Missing or invalid API key / authentication error.
    - 'model_not_found': Model does not exist or unknown model identifier.
    - 'rate_limit': 429 Too Many Requests, quota exceeded, or rate limit crossed.
    - 'general_error': Any other unexpected runtime exception.
    """
    error_str = str(e).lower()
    error_type = type(e).__name__.lower()

    # 1. Rate Limit Checks
    if (
        "ratelimit" in error_type
        or "rate_limit" in error_str
        or "ratelimit" in error_str
        or "rate limit" in error_str
        or "429" in error_str
        or "too many requests" in error_str
        or "resourceexhausted" in error_str
        or "resource_exhausted" in error_str
        or "quota" in error_str
        or "tpm" in error_str
        or "rpm" in error_str
    ):
        return "rate_limit"

    # 2. API Key Not Found / Authentication Checks
    if (
        "auth" in error_type
        or "api_key" in error_str
        or "apikey" in error_str
        or "api key" in error_str
        or "authentication" in error_str
        or "unauthorized" in error_str
        or "401" in error_str
        or "forbidden" in error_str
        or "403" in error_str
    ):
        return "api_key_not_found"

    # 3. Model Not Found Checks
    if (
        "notfound" in error_type
        or "model_not_found" in error_str
        or "model not found" in error_str
        or "does not exist" in error_str
        or "is not found" in error_str
        or "invalid model" in error_str
        or "unknown model" in error_str
        or "404" in error_str
    ):
        return "model_not_found"

    return "general_error"


def is_stop_command(raw_text: str, is_mention_or_dm: bool = False) -> bool:
    """
    Detects if the message is an explicit /stop command directed at the bot.
    Supports:
    - /stop
    - @CaiLogics Hub /stop, @CaiLogics /stop, <@BOT_ID> /stop
    - @Agent_Key /stop or [Agent_Key] /stop
    - Variations like '/stop discussion', '/stop flow', '/stop please', '/stani', '/zaustavi', '/prekini'
    """
    if not raw_text:
        return False

    # Check for literal '@app_mention /stop'
    if re.search(r"@app_mention\s+/stop\b", raw_text, re.IGNORECASE):
        return True

    # Clean Slack user / bot mentions like <@U123456>
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    # Clean hub / bot names like @CaiLogics Hub, @CaiLogics Test, @CaiLogics
    cleaned = re.sub(
        r"@?CaiLogics\s+(Hub|Test|Multi-Agent|AgenticPanel|Panel)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"@?CaiLogics\b", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"@app_mention\b", "", cleaned, flags=re.IGNORECASE).strip()

    # Clean individual agent keys / personas
    for key, cfg in AGENTS_CONFIG.items():
        cleaned = re.sub(
            r"@?" + re.escape(key), "", cleaned, flags=re.IGNORECASE
        ).strip()
        persona = cfg.get("username", "")
        if persona:
            cleaned = re.sub(
                r"@?" + re.escape(persona), "", cleaned, flags=re.IGNORECASE
            ).strip()
        # Also remove bracketed agent tags like [Gemini_Visionary] /stop
        cleaned = re.sub(
            r"\[" + re.escape(key) + r"\]", "", cleaned, flags=re.IGNORECASE
        ).strip()

    # Normalize punctuation & whitespace, preserving '/'
    normalized = re.sub(r"[^\w\s/]", "", cleaned).strip().lower()

    stop_phrases = {
        "/stop",
        "/stop discussion",
        "/stop panel",
        "/stop flow",
        "/stop please",
        "please /stop",
        "/halt",
        "/cancel",
        "/terminate",
        "/stani",
        "/zaustavi",
        "/prekini",
    }
    if normalized in stop_phrases:
        return True

    words = normalized.split()
    return bool(
        words
        and words[0]
        in (
            "/stop",
            "/stani",
            "/zaustavi",
            "/prekini",
            "/halt",
            "/cancel",
            "/terminate",
        )
        and len(words) <= 3
    )


def request_thread_stop(channel_id: str, thread_ts: str) -> None:
    """Marks the thread for stopping and clears remaining queued agents."""
    thread_key = (channel_id, thread_ts)
    STOP_REQUESTED_THREADS.add(thread_key)
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
    THREAD_HISTORY_LIMITS.pop(thread_key, None)
    CUSTOM_FLOW_THREADS.discard(thread_key)
    logger.info(
        f"[ROUTER] Stop requested for thread {thread_key}. REMAINING_AGENTS_MAP, retry queues, and history limits cleared."
    )
    try:
        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=True,
        )
        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=True,
        )
    except Exception as e:
        logger.debug(f"[ROUTER] Could not finalize thread note or usage on stop: {e}")


def is_thread_stopped(channel_id: str, thread_ts: str) -> bool:
    """Checks if a stop has been requested for this thread."""
    return (channel_id, thread_ts) in STOP_REQUESTED_THREADS


def clear_thread_stop(channel_id: str, thread_ts: str) -> None:
    """Clears the stop flag for this thread."""
    STOP_REQUESTED_THREADS.discard((channel_id, thread_ts))


# Mandatory locked agents that cannot be turned off
LOCKED_AGENTS = {"Gemini_Visionary", "OpAiTerra_Architect", "AnthrOpus_Sceptic"}

# Global in-memory set of enabled debate agents (default: all agents in AGENTS_CONFIG)
ENABLED_DEBATE_AGENTS: set[str] = set(AGENTS_CONFIG.keys())

# Circuit-Breaker configuration for thread expense limits
CIRCUIT_BREAKER_ENABLED: bool = False
CIRCUIT_BREAKER_THRESHOLD_USD: float = 1.0


def is_circuit_breaker_enabled() -> bool:
    """Returns True if the thread expense circuit-breaker is enabled."""
    return CIRCUIT_BREAKER_ENABLED


def set_circuit_breaker_enabled(enabled: bool, save: bool = True) -> None:
    """Enables or disables the thread expense circuit-breaker and persists settings."""
    global CIRCUIT_BREAKER_ENABLED
    CIRCUIT_BREAKER_ENABLED = bool(enabled)
    if save:
        save_enabled_agents()
    logger.info(
        f"[ROUTER] Circuit-breaker enabled set to {CIRCUIT_BREAKER_ENABLED}."
    )


def get_circuit_breaker_threshold() -> float:
    """Returns the current monetary threshold in USD for the circuit-breaker."""
    return float(CIRCUIT_BREAKER_THRESHOLD_USD)


def set_circuit_breaker_threshold(threshold: float, save: bool = True) -> None:
    """Sets the monetary threshold in USD for the circuit-breaker and persists settings."""
    global CIRCUIT_BREAKER_THRESHOLD_USD
    try:
        val = float(threshold)
        if val < 0.0:
            val = 0.0
        CIRCUIT_BREAKER_THRESHOLD_USD = val
        if save:
            save_enabled_agents()
        logger.info(
            f"[ROUTER] Circuit-breaker threshold set to ${CIRCUIT_BREAKER_THRESHOLD_USD:.2f} USD."
        )
    except (ValueError, TypeError) as e:
        logger.error(f"[ROUTER] Invalid circuit-breaker threshold '{threshold}': {e}")


def get_circuit_breaker_config() -> dict[str, Any]:
    """Returns the current circuit-breaker configuration dictionary."""
    return {
        "enabled": CIRCUIT_BREAKER_ENABLED,
        "threshold_usd": CIRCUIT_BREAKER_THRESHOLD_USD,
    }


def save_enabled_agents() -> None:
    """Saves the current enabled debate agents and circuit-breaker settings to a persistent JSON configuration file."""
    try:
        data = {
            "enabled_agents": sorted(list(ENABLED_DEBATE_AGENTS)),
            "circuit_breaker_enabled": CIRCUIT_BREAKER_ENABLED,
            "circuit_breaker_threshold_usd": CIRCUIT_BREAKER_THRESHOLD_USD,
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(
            f"[ROUTER] Saved settings ({len(ENABLED_DEBATE_AGENTS)} enabled agents, circuit_breaker={CIRCUIT_BREAKER_ENABLED}, threshold=${CIRCUIT_BREAKER_THRESHOLD_USD:.2f}) to {SETTINGS_FILE.name}"
        )
    except Exception as e:
        logger.error(f"[ROUTER] Error saving agent settings: {e}")


def load_enabled_agents() -> set[str]:
    """
    Loads enabled debate agents and circuit-breaker settings from persistent JSON file if available.
    Ensures locked agents are always included and unknown keys are filtered.
    Falls back to default (all agents ON, circuit_breaker OFF) if no settings file exists.
    """
    global CIRCUIT_BREAKER_ENABLED, CIRCUIT_BREAKER_THRESHOLD_USD
    ENABLED_DEBATE_AGENTS.clear()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = set(data.get("enabled_agents", []))
            # Validate keys against AGENTS_CONFIG
            valid_keys = {k for k in loaded if k in AGENTS_CONFIG}
            # Ensure locked agents are always present
            valid_keys.update(LOCKED_AGENTS)
            ENABLED_DEBATE_AGENTS.update(valid_keys)

            CIRCUIT_BREAKER_ENABLED = bool(data.get("circuit_breaker_enabled", False))
            CIRCUIT_BREAKER_THRESHOLD_USD = float(
                data.get("circuit_breaker_threshold_usd", 1.0)
            )

            logger.info(
                f"[ROUTER] Loaded settings from {SETTINGS_FILE.name}: {len(ENABLED_DEBATE_AGENTS)} agents, circuit_breaker={CIRCUIT_BREAKER_ENABLED}, threshold=${CIRCUIT_BREAKER_THRESHOLD_USD:.2f}"
            )
            return set(ENABLED_DEBATE_AGENTS)
        except Exception as e:
            logger.error(f"[ROUTER] Error loading agent settings file: {e}")

    # Fallback default: all agents enabled
    ENABLED_DEBATE_AGENTS.update(AGENTS_CONFIG.keys())
    CIRCUIT_BREAKER_ENABLED = False
    CIRCUIT_BREAKER_THRESHOLD_USD = 1.0
    save_enabled_agents()
    return set(ENABLED_DEBATE_AGENTS)


def reset_enabled_agents(save: bool = True) -> None:
    """Resets all agents to enabled state."""
    ENABLED_DEBATE_AGENTS.clear()
    ENABLED_DEBATE_AGENTS.update(AGENTS_CONFIG.keys())
    if save:
        save_enabled_agents()
    logger.info(f"[ROUTER] Reset all {len(ENABLED_DEBATE_AGENTS)} agents to ENABLED.")


def get_enabled_agents() -> set[str]:
    """Returns a copy of currently enabled agents."""
    return set(ENABLED_DEBATE_AGENTS)


def set_agent_enabled(agent_key: str, enabled: bool) -> bool:
    """
    Enables or disables an agent for automatic debate.
    Locked agents (Visionary, Architect, Sceptic) cannot be disabled.
    Persists changes to agent_settings.json.
    Returns True if state was successfully updated, False otherwise.
    """
    if agent_key in LOCKED_AGENTS and not enabled:
        logger.warning(
            f"[ROUTER] Cannot disable locked agent '{agent_key}'. Ignoring request."
        )
        return False

    if agent_key not in AGENTS_CONFIG:
        logger.warning(f"[ROUTER] Unknown agent key '{agent_key}'.")
        return False

    if enabled:
        ENABLED_DEBATE_AGENTS.add(agent_key)
    else:
        ENABLED_DEBATE_AGENTS.discard(agent_key)
    save_enabled_agents()
    logger.info(
        f"[ROUTER] Set agent '{agent_key}' enabled={enabled}. Total enabled: {len(ENABLED_DEBATE_AGENTS)}"
    )
    return True


def toggle_agent(agent_key: str) -> bool:
    """Toggles an agent's enabled state. Returns the new enabled state."""
    current_state = agent_key in ENABLED_DEBATE_AGENTS
    new_state = not current_state
    set_agent_enabled(agent_key, new_state)
    return agent_key in ENABLED_DEBATE_AGENTS


def get_initial_agents() -> list[str]:
    """
    Returns the list of enabled optional agents that should speak in the discussion flow.
    Locked agents like AnthrOpus_Sceptic are always included.
    """
    return [
        key
        for key in AGENTS_CONFIG
        if key in ENABLED_DEBATE_AGENTS
        and key not in ("OpAiTerra_Architect", "Gemini_Visionary")
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
    _, cleaned_text = extract_history_limit(text)
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", cleaned_text).strip()

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
    pending_retries: list[str] | None = None,
) -> str:
    """
    Ensures that the agent's response nominates a valid agent:
    1. If force_termination or current agent is OpAiTerra_Architect, no nomination is required.
    2. If remaining agents exist:
       - Preserves the agent's nomination if it chose an agent that is still in `remaining`.
       - If the agent prematurely nominated OpAiTerra_Architect or an agent that already spoke,
         programmatically replaces the nomination tag with the first remaining agent (remaining[0]).
       - If no nomination tag is present, appends [remaining[0]].
    3. If remaining list is empty:
       - If pending rate-limit retries exist, nominates the pending retried agent.
       - If no pending retries and current agent is not OpAiTerra_Architect, nominates [OpAiTerra_Architect].
    """
    if force_termination or current_agent_key == "OpAiTerra_Architect":
        # Strip any bracketed agent tags so Slack webhook won't trigger unexpected bot loops
        for key in AGENTS_CONFIG:
            text = re.sub(
                r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE
            )
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
        # remaining list is empty -> check pending_retries
        retries_list = pending_retries or []
        if retries_list:
            valid_retry = None
            for match, key in agent_tag_matches:
                if key in retries_list:
                    valid_retry = key
                    break
            if valid_retry:
                logger.info(
                    f"[ROUTER] Agent {current_agent_key} nominated valid rate-limit retry agent [{valid_retry}]."
                )
                return text

            fallback_retry = retries_list[0]
            if agent_tag_matches:
                last_match, last_key = agent_tag_matches[-1]
                logger.info(
                    f"[ROUTER] Correcting nomination to pending rate-limit retry agent [{fallback_retry}]."
                )
                start, end = last_match.span()
                return text[:start] + f"[{fallback_retry}]" + text[end:]
            else:
                logger.info(
                    f"[ROUTER] Appending pending rate-limit retry agent [{fallback_retry}]."
                )
                return f"{text.strip()}\n\n[{fallback_retry}]"
        else:
            # remaining list and retries are empty -> next must be OpAiTerra_Architect
            if agent_tag_matches:
                last_match, last_key = agent_tag_matches[-1]
                if last_key == "OpAiTerra_Architect":
                    return text
                logger.info(
                    f"[ROUTER] All intermediate & retried agents have spoken. Correcting tag '[{last_key}]' to [OpAiTerra_Architect]."
                )
                start, end = last_match.span()
                return text[:start] + "[OpAiTerra_Architect]" + text[end:]
            else:
                logger.info(
                    f"[ROUTER] All intermediate & retried agents have spoken. Appending [OpAiTerra_Architect] for final summary."
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


def build_llm_messages(
    system_prompt: str,
    effective_history: list[dict[str, Any]],
    prompt: str,
    is_bot_trigger: bool,
    agent_key: str,
    agent_cfg: dict[str, Any],
    file_context: dict[str, Any] | None = None,
    remaining_agents: list[str] | None = None,
    force_termination: bool = False,
) -> list[dict[str, Any]]:
    """
    Constructs a normalized, provider-safe message sequence for LiteLLM.
    Guarantees:
    1. System prompt is at index 0 (100% static for robust Anthropic prompt caching).
    2. Dynamic orchestration context (available agents list / termination constraints)
       is appended directly to the final user prompt (benefiting from LLM recency attention).
    3. The conversational sequence starts with a 'user' turn (handles trimmed history).
    4. The conversational sequence strictly ends with a 'user' turn (never 'assistant'),
       which is strictly required by Anthropic (Claude), Mistral, and Google Gemini APIs.
    5. Injects on-demand attached file content if present and if the current agent is authorized.
    6. Formats system message content block with cache_control for Anthropic models.
    """
    conversation: list[dict[str, Any]] = []

    # Check authorization for attached file
    file_block = ""
    if file_context:
        authorized_agents = file_context.get("authorized_agents")
        if authorized_agents is None or agent_key in authorized_agents:
            file_name = file_context.get("file_name", "unknown")
            content = file_context.get("content", "")
            size = file_context.get("size", 0)
            file_block = (
                f"\n\n--- [ATTACHED SLACK FILE: {file_name} ({size} bytes)] ---\n"
                f"{content}\n"
                f"--- [END OF ATTACHED FILE] ---"
            )

    # Dynamic orchestration directive
    orchestration_block = ""
    if force_termination:
        orchestration_block = (
            "\n\nCRITICAL CONSTRAINT: You are the last agent requested to speak in this turn. "
            "Do NOT nominate any other agent to speak next. Do NOT include any bracketed agent key (like [Mistral_Innovator] "
            "or [OpAiTerra_Architect]) at the end of your response. Simply answer the question/summarize and finish your response."
        )
    elif remaining_agents:
        orchestration_block = (
            f"\n\nAVAILABLE AGENTS WHO HAVE NOT SPOKEN YET (nominate the next agent at the very end in [AGENT_KEY] format):\n"
            f"{', '.join(remaining_agents)}"
        )

    if not is_bot_trigger:
        if effective_history:
            conversation.extend(effective_history)
        user_prompt = f"{prompt}{file_block}{orchestration_block}"
        conversation.append({"role": "user", "content": user_prompt})
    else:
        if effective_history:
            conversation.extend(effective_history)
            persona_name = agent_cfg.get("username", agent_key)
            instruction = (
                f"You have been called to speak as {persona_name}. "
                "Please evaluate the discussion above and provide your response in accordance with your role and instructions."
            )
            instruction = f"{instruction}{file_block}{orchestration_block}"
            conversation.append(
                {
                    "role": "user",
                    "content": instruction,
                }
            )
        else:
            user_prompt = f"{prompt}{file_block}{orchestration_block}"
            conversation.append({"role": "user", "content": user_prompt})

    # Ensure the first conversational message has role 'user' (for strict APIs like Anthropic/Gemini)
    if conversation and conversation[0]["role"] != "user":
        conversation.insert(
            0,
            {
                "role": "user",
                "content": "Context from previous discussion in thread:",
            },
        )

    # Ensure the last conversational message has role 'user'
    if conversation and conversation[-1]["role"] != "user":
        persona_name = agent_cfg.get("username", agent_key)
        instruction = (
            f"You have been called to speak as {persona_name}. "
            "Please evaluate the discussion above and provide your response in accordance with your role and instructions."
        )
        instruction = f"{instruction}{file_block}{orchestration_block}"
        conversation.append(
            {
                "role": "user",
                "content": instruction,
            }
        )

    model_name = agent_cfg.get("model")
    if is_anthropic_model(model_name):
        system_item: dict[str, Any] = {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }

        # 1 breakpoint used for system prompt. Up to 3 remaining (Anthropic max 4 total).
        has_file_cache = bool(file_block)
        if has_file_cache and conversation:
            # Structure last user turn into a cached file block + dynamic instruction block
            active_turn = conversation[-1]
            instruction_text = (
                f"{prompt}{orchestration_block}"
                if not is_bot_trigger
                else f"You have been called to speak as {agent_cfg.get('username', agent_key)}. Please evaluate the discussion above and provide your response in accordance with your role and instructions.{orchestration_block}"
            )
            active_turn["content"] = [
                {
                    "type": "text",
                    "text": file_block.strip(),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": instruction_text,
                },
            ]

        # Remaining cache slots for early thread history (2 if file is cached, 3 otherwise)
        history_slots = 2 if has_file_cache else 3
        num_history = max(0, len(conversation) - 1)
        for i in range(min(num_history, history_slots)):
            msg_content = conversation[i].get("content")
            if isinstance(msg_content, str):
                conversation[i]["content"] = [
                    {
                        "type": "text",
                        "text": msg_content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif isinstance(msg_content, list) and msg_content:
                msg_content[-1]["cache_control"] = {"type": "ephemeral"}
    else:
        system_item = {"role": "system", "content": system_prompt}

    return [system_item] + conversation


async def _execute_llm_with_retry(
    func: Any,
    kwargs: dict[str, Any],
    agent_key: str,
    thread_key: tuple[str, str],
    is_superseded_fn=None,
    max_attempts: int = LLM_MAX_RETRIES,
) -> Any:
    """
    Executes an LLM call in a threadpool with automatic retries and exponential backoff.
    Attempts up to `max_attempts` times before raising the last exception.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if is_superseded_fn and is_superseded_fn():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded before LLM attempt {attempt}. Aborting."
            )
            return None

        try:
            if attempt > 1:
                logger.info(
                    f"[LLM] Retrying LLM completion for {agent_key} (attempt {attempt}/{max_attempts})..."
                )
            return await run_in_threadpool(func, **kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{max_attempts} failed for {agent_key}: {exc}"
            )
            if attempt < max_attempts:
                await asyncio.sleep(LLM_RETRY_BACKOFF_BASE * attempt)
            else:
                raise last_error


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

    thread_key = (channel_id, thread_ts)

    if not is_bot_trigger:
        THREAD_GENERATIONS[thread_key] = THREAD_GENERATIONS.get(thread_key, 0) + 1

    my_generation = THREAD_GENERATIONS.get(thread_key, 0)
    current_task = asyncio.current_task()
    if current_task:
        ACTIVE_THREAD_TASKS[thread_key] = current_task

    def is_superseded() -> bool:
        if THREAD_GENERATIONS.get(thread_key, 0) != my_generation:
            return True
        if current_task and current_task.cancelled():
            return True
        return False

    history_limit, raw_text_cleaned = extract_history_limit(raw_text)
    max_words_limit, raw_text_cleaned = extract_max_words_limit(raw_text_cleaned)

    # Adjust remaining agents list and file instructions if user input
    if not is_bot_trigger:
        clear_thread_stop(channel_id, thread_ts)
        if max_words_limit is not None:
            set_thread_max_words(channel_id, thread_ts, max_words_limit)
        thread_files = await fetch_thread_files(channel_id, thread_ts)
        is_file_req, target_filename, raw_text_cleaned = extract_file_instruction(
            raw_text_cleaned, available_files=thread_files
        )

        agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
            raw_text_cleaned, channel_id, thread_ts
        )

        tagged = extract_tagged_agents(raw_text_cleaned)
        if tagged:
            agent_key = tagged[0]
            remaining_tagged = [a for a in tagged if a != agent_key]
            REMAINING_AGENTS_MAP[thread_key] = remaining_tagged
            CUSTOM_FLOW_THREADS.add(thread_key)
            was_explicit_target = True
        else:
            CUSTOM_FLOW_THREADS.discard(thread_key)

        if is_file_req:
            # Determine authorized agents:
            # If user explicitly tagged agents, only those tagged agents are authorized
            # If no agent was tagged, authorized_agents is None (all agents authorized)
            if tagged:
                authorized_agents = set(tagged)
            elif was_explicit_target:
                authorized_agents = {agent_key}
            else:
                authorized_agents = None

            chosen_file = resolve_target_file(thread_files, target_filename)
            if chosen_file:
                logger.info(
                    f"[ROUTER] User requested file reading for thread {thread_key}. Downloading file: {chosen_file.get('name')}"
                )
                dl_res = await download_slack_file(chosen_file)
                if dl_res.get("ok"):
                    set_thread_file_context(
                        channel_id,
                        thread_ts,
                        {
                            "file_name": dl_res.get(
                                "file_name", chosen_file.get("name")
                            ),
                            "content": dl_res.get("content", ""),
                            "size": dl_res.get("size", 0),
                            "truncated": dl_res.get("truncated", False),
                            "authorized_agents": authorized_agents,
                        },
                    )
                else:
                    set_thread_file_context(
                        channel_id,
                        thread_ts,
                        {
                            "file_name": chosen_file.get("name", "unknown"),
                            "content": f"[Error reading file: {dl_res.get('error')}]",
                            "size": chosen_file.get("size", 0),
                            "truncated": False,
                            "authorized_agents": authorized_agents,
                        },
                    )
            else:
                logger.warning(
                    f"[ROUTER] User requested file reading but no files were found in thread {thread_key}"
                )
                set_thread_file_context(
                    channel_id,
                    thread_ts,
                    {
                        "file_name": target_filename or "unknown",
                        "content": "[No attached file found in this Slack thread to read.]",
                        "size": 0,
                        "truncated": False,
                        "authorized_agents": authorized_agents,
                    },
                )
        else:
            clear_thread_file_context(channel_id, thread_ts)
    else:
        agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
            raw_text_cleaned, channel_id, thread_ts
        )

    agent_cfg = AGENTS_CONFIG.get(agent_key, AGENTS_CONFIG[DEFAULT_AGENT_KEY])

    logger.info(
        f"[ROUTER] Parsed Agent: {agent_key} ({agent_cfg['model']}) | Explicit target: {was_explicit_target} | Bot trigger: {is_bot_trigger} | Gen: {my_generation} | History limit: {history_limit}"
    )

    if is_superseded():
        logger.info(f"[ROUTER] Thread {thread_key} turn was superseded. Aborting.")
        return {"ok": False, "cancelled": True, "reason": "superseded"}

    # Circuit-breaker verification: Check if cumulative thread cost reached or exceeded threshold
    if is_circuit_breaker_enabled():
        cost_info = calculate_thread_costs(channel_id, thread_ts)
        current_cost = cost_info.get("grand_total_cost_usd", 0.0)
        threshold = get_circuit_breaker_threshold()
        if current_cost >= threshold:
            logger.warning(
                f"[CIRCUIT BREAKER] Thread {thread_key} cost (${current_cost:.5f}) reached or exceeded threshold (${threshold:.5f}). Interrupting response."
            )
            circuit_breaker_msg = "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
            slack_res = await post_agent_response(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=circuit_breaker_msg,
                username=agent_cfg["username"],
                icon_url=agent_cfg.get("icon_url"),
            )
            await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)
            trigger_thread_notes_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
                bot_user_id=bot_user_id,
            )
            trigger_usage_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
            )
            clear_thread_stop(channel_id, thread_ts)
            clear_thread_history_limit(channel_id, thread_ts)
            clear_thread_max_words(channel_id, thread_ts)
            clear_thread_file_context(channel_id, thread_ts)
            CUSTOM_FLOW_THREADS.discard(thread_key)
            REMAINING_AGENTS_MAP.pop(thread_key, None)
            RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
            return {
                "ok": True,
                "circuit_breaker_triggered": True,
                "slack_res": slack_res,
            }

    # 1. Fetch thread history context
    history = await fetch_thread_history(channel_id, thread_ts, bot_user_id=bot_user_id)
    logger.info(f"[ROUTER] Fetched {len(history)} messages from thread history.")

    if is_superseded():
        logger.info(f"[ROUTER] Thread {thread_key} turn was superseded. Aborting.")
        return {"ok": False, "cancelled": True, "reason": "superseded"}

    # Determine effective conversation history based on active /history limit
    if not is_bot_trigger:
        # User trigger: identify prior messages before this prompt in thread history
        # If Slack already included the user prompt at history[-1], prior messages are history[:-1]
        if history and history[-1]["role"] == "user":
            prior_messages = history[:-1]
        else:
            prior_messages = history

        if history_limit is not None:
            set_thread_history_limit(
                channel_id=channel_id,
                thread_ts=thread_ts,
                limit=history_limit,
                prior_cutoff_index=len(prior_messages),
            )
            effective_prior = (
                prior_messages[-history_limit:] if history_limit > 0 else []
            )
            logger.info(
                f"[ROUTER] Applying /history {history_limit}: kept {len(effective_prior)} of {len(prior_messages)} prior messages for user prompt."
            )
        else:
            clear_thread_history_limit(channel_id, thread_ts)
            effective_prior = prior_messages

        effective_history = effective_prior
    else:
        # Bot trigger (subsequent automated debate rounds):
        hist_limit_info = get_thread_history_limit(channel_id, thread_ts)
        if hist_limit_info is not None:
            limit = hist_limit_info["limit"]
            cutoff = hist_limit_info["prior_cutoff_index"]
            prior_part = history[:cutoff]
            discussion_part = history[cutoff:]
            effective_prior = prior_part[-limit:] if limit > 0 else []
            effective_history = effective_prior + discussion_part
            logger.info(
                f"[ROUTER] Applying active history limit ({limit}): kept {len(effective_prior)}/{len(prior_part)} prior messages + {len(discussion_part)} discussion messages."
            )
        else:
            effective_history = history

    # 2. Build message sequence for LiteLLM
    active_max_words = get_thread_max_words(channel_id, thread_ts)
    if active_max_words is not None:
        logger.info(
            f"[ROUTER] Applying /max_words limit ({active_max_words}) for agent {agent_key} (skipping standard output_constraints)."
        )
        system_prompt = build_agent_prompt(agent_key, max_words=active_max_words)
    else:
        system_prompt = agent_cfg["system_prompt"]

    remaining = REMAINING_AGENTS_MAP.get(thread_key, get_initial_agents())
    is_custom_flow = thread_key in CUSTOM_FLOW_THREADS

    # Determine if we should force termination (no more nominations)
    force_termination = False
    if was_explicit_target and not is_bot_trigger and not remaining:
        force_termination = True
    elif is_custom_flow and not remaining:
        force_termination = True
    elif is_thread_stopped(channel_id, thread_ts):
        force_termination = True

    if force_termination:
        logger.info(
            f"[ROUTER] Agent {agent_key} forcing termination (no remaining agents in sequence or stop requested)."
        )
    else:
        logger.info(
            f"[ROUTER] Agent {agent_key} remaining available agents: {remaining}"
        )

    file_context = get_thread_file_context(channel_id, thread_ts)
    messages = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=effective_history,
        prompt=prompt,
        is_bot_trigger=is_bot_trigger,
        agent_key=agent_key,
        agent_cfg=agent_cfg,
        file_context=file_context,
        remaining_agents=remaining,
        force_termination=force_termination,
    )

    try:
        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded before LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cached_tokens = 0

        def add_usage(resp, context: str = ""):
            nonlocal total_prompt_tokens, total_completion_tokens, total_cached_tokens
            if not resp:
                return
            log_llm_cache_usage(
                agent_cfg.get("model"), resp, context or agent_key, logger
            )
            model = agent_cfg.get("model")
            if is_openai_model(model):
                total_cached_tokens += extract_cached_tokens(resp)
            elif is_anthropic_model(model):
                total_cached_tokens += extract_anthropic_cache_usage(resp).get("cache_read", 0)

            usage = getattr(resp, "usage", None)
            if not usage:
                return
            if isinstance(usage, dict):
                total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
                total_completion_tokens += usage.get("completion_tokens", 0) or 0
            else:
                total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0

        # 3. Call LiteLLM model with MCP tools in a threadpool with automatic retries
        logger.info(f"[LLM] Dispatching litellm.completion for {agent_key}...")
        llm_start = time.time()
        completion_kwargs: dict[str, Any] = {
            "model": agent_cfg["model"],
            "messages": messages,
            # "temperature": agent_cfg["temperature"],
            "tools": MCP_TOOLS_SCHEMAS,
            "tool_choice": "auto",
            "num_retries": 2,
        }
        response = await _execute_llm_with_retry(
            litellm.completion,
            completion_kwargs,
            agent_key=agent_key,
            thread_key=thread_key,
            is_superseded_fn=is_superseded,
        )
        if response is None and is_superseded():
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        add_usage(response)
        llm_duration = time.time() - llm_start
        logger.info(
            f"[LLM] Received response for {agent_key} in {llm_duration:.2f} seconds."
        )

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded after LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

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

            if is_superseded():
                logger.info(
                    f"[ROUTER] Thread {thread_key} turn was superseded during tool execution. Aborting."
                )
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            # Follow-up completion after tool results in a threadpool with automatic retries
            follow_up_kwargs: dict[str, Any] = {
                "model": agent_cfg["model"],
                "messages": messages,
                "num_retries": 2,
                # "temperature": agent_cfg["temperature"],
            }
            follow_up = await _execute_llm_with_retry(
                litellm.completion,
                follow_up_kwargs,
                agent_key=f"{agent_key} follow-up",
                thread_key=thread_key,
                is_superseded_fn=is_superseded,
            )
            if follow_up is None and is_superseded():
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            add_usage(follow_up, f"{agent_key} follow-up")
            final_text = follow_up.choices[0].message.content
        else:
            final_text = response_message.content

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded before pacing delay. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        # Re-check stop flag in case a stop was requested while LLM was processing
        if is_thread_stopped(channel_id, thread_ts):
            force_termination = True
            logger.info(
                f"[ROUTER] Thread {thread_key} received stop request during execution. Forcing termination."
            )

        retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
        if not final_text:
            if force_termination:
                final_text = "(No text response generated)"
            else:
                if remaining:
                    next_tag = remaining[0]
                elif retries:
                    next_tag = retries[0]
                else:
                    next_tag = "OpAiTerra_Architect"
                final_text = f"(No text response generated) [{next_tag}] next"
            logger.warning(f"[ROUTER] final_text was empty for {agent_key}!")
        else:
            final_text = markdown_to_slack(final_text)
            final_text = enforce_valid_nomination(
                final_text,
                agent_key,
                [] if force_termination else remaining,
                force_termination,
                pending_retries=retries,
            )

        # If a pending retry agent was nominated, consume it from RATE_LIMIT_RETRY_QUEUE and track
        retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
        if not remaining and retries:
            for r_agent in list(retries):
                if f"[{r_agent}]" in final_text:
                    retries.remove(r_agent)
                    RATE_LIMIT_ALREADY_RETRIED.setdefault(thread_key, set()).add(
                        r_agent
                    )
                    break

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
        await asyncio.sleep(3.0)

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

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

        # Short pause after Slack post to maintain pacing and avoid rate limits
        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        # Determine if this turn completes the thread discussion
        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        is_completed = bool(
            force_termination
            or (
                agent_key == "OpAiTerra_Architect"
                and not remaining
                and not has_pending_retries
            )
            or (is_thread_stopped(channel_id, thread_ts))
        )

        # Record token consumption for this agent's model
        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=agent_cfg["model"],
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cached_tokens=total_cached_tokens,
        )

        # Trigger parallel, non-blocking live thread notes update after posting reply
        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

        # Trigger parallel, non-blocking live usage report update in #cailogics-usage
        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )

        # Reset remaining agents/custom flows for this thread when finishing
        if force_termination:
            logger.info(
                f"[ROUTER] Thread {thread_key} reached termination (forced or natural). Cleaning up state."
            )
            clear_thread_stop(channel_id, thread_ts)
            clear_thread_history_limit(channel_id, thread_ts)
            clear_thread_max_words(channel_id, thread_ts)
            clear_thread_file_context(channel_id, thread_ts)
            CUSTOM_FLOW_THREADS.discard(thread_key)
            REMAINING_AGENTS_MAP.pop(thread_key, None)
            RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
        elif (
            agent_key == "OpAiTerra_Architect"
            and not remaining
            and not has_pending_retries
        ):
            logger.info(
                f"[ROUTER] OpAiTerra_Architect finished! Deleting state maps for thread {thread_key}"
            )
            clear_thread_history_limit(channel_id, thread_ts)
            clear_thread_max_words(channel_id, thread_ts)
            clear_thread_file_context(channel_id, thread_ts)
            REMAINING_AGENTS_MAP.pop(thread_key, None)
            RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {thread_ts} (Total: {time.time() - start_time:.2f}s) <<<\n"
        )
        return slack_res

    except asyncio.CancelledError:
        logger.info(
            f"[ROUTER] Task cancelled for thread {thread_key} (agent {agent_key}, gen {my_generation}). Aborting cleanly."
        )
        return {"ok": False, "cancelled": True, "reason": "cancelled"}
    except Exception as e:
        if is_superseded():
            logger.info(
                f"[ROUTER] Exception occurred on superseded turn for thread {thread_key}. Aborting without posting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        logger.exception(f"[ERROR] Exception during agent completion or post: {e}")
        err_category = classify_agent_error(e)
        logger.info(f"[ROUTER] Classified error for {agent_key} as: '{err_category}'")

        stop_requested = is_thread_stopped(channel_id, thread_ts)

        # Build single sentence problem statement and manage rate-limit retry queue
        if err_category == "api_key_not_found":
            error_sentence = "API KEY NOT FOUND"
        elif err_category == "model_not_found":
            error_sentence = "MODEL NOT FOUND"
        elif err_category == "rate_limit":
            already_retried = agent_key in RATE_LIMIT_ALREADY_RETRIED.get(
                thread_key, set()
            )
            if not already_retried and not stop_requested:
                error_sentence = (
                    "RATE LIMITS CROSSED, will retry at the end of discussion"
                )
                RATE_LIMIT_RETRY_QUEUE.setdefault(thread_key, []).append(agent_key)
            else:
                error_sentence = "RATE LIMITS CROSSED"
        else:
            error_sentence = "Error processing request - check the logs"

        # Determine next nomination to ensure thread continuity
        next_nomination = None
        if not stop_requested:
            if remaining:
                next_agent = remaining[0]
                next_nomination = f"[{next_agent}]"
            else:
                retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
                if retries:
                    next_agent = retries.pop(0)
                    RATE_LIMIT_ALREADY_RETRIED.setdefault(thread_key, set()).add(
                        next_agent
                    )
                    next_nomination = f"[{next_agent}]"
                elif agent_key != "OpAiTerra_Architect" and not (
                    was_explicit_target and not is_bot_trigger
                ):
                    next_nomination = "[OpAiTerra_Architect]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        # Post response back to Slack thread with the responsible agent's persona
        logger.info(
            f"[SLACK] Posting gracious error message to channel {channel_id}, thread_ts {thread_ts} as {agent_cfg['username']}"
        )
        slack_res = await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=error_post_text,
            username=agent_cfg["username"],
            icon_url=agent_cfg.get("icon_url"),
        )

        # Short pause after posting error message to prevent rapid cascading message spam
        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        is_completed = bool(
            force_termination
            or (
                agent_key == "OpAiTerra_Architect"
                and not remaining
                and not has_pending_retries
            )
            or not next_nomination
            or is_thread_stopped(channel_id, thread_ts)
        )

        # Record 0 tokens for failed turn
        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=agent_cfg["model"],
            prompt_tokens=0,
            completion_tokens=0,
        )

        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )

        if is_completed:
            clear_thread_stop(channel_id, thread_ts)
            clear_thread_history_limit(channel_id, thread_ts)
            clear_thread_max_words(channel_id, thread_ts)
            clear_thread_file_context(channel_id, thread_ts)
            CUSTOM_FLOW_THREADS.discard(thread_key)
            REMAINING_AGENTS_MAP.pop(thread_key, None)
            RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
        }
    finally:
        if current_task and ACTIVE_THREAD_TASKS.get(thread_key) == current_task:
            ACTIVE_THREAD_TASKS.pop(thread_key, None)
