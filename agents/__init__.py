"""
agents package - Special purpose background and support agents for CaiLogics Slack Platform.
"""

from .thread_notes_agent import (
    THREAD_LOCKS as NOTES_THREAD_LOCKS,
    THREAD_NOTE_MSG_MAP,
    clear_thread_note,
    generate_thread_notes,
    get_thread_lock as get_notes_thread_lock,
    get_thread_note,
    set_thread_note,
    trigger_thread_notes_update,
    update_notes_for_thread,
)
from .usage_agent import (
    THREAD_USAGE_DATA,
    USAGE_LOCKS,
    USAGE_STORE_FILE,
    calculate_thread_costs,
    format_usage_slack_message,
    load_thread_usage_data,
    record_thread_tokens,
    save_thread_usage_data,
    trigger_usage_update,
    update_usage_for_thread,
)

__all__ = [
    # Thread Notes / Scribe Agent
    "THREAD_NOTE_MSG_MAP",
    "NOTES_THREAD_LOCKS",
    "get_notes_thread_lock",
    "get_thread_note",
    "set_thread_note",
    "clear_thread_note",
    "generate_thread_notes",
    "update_notes_for_thread",
    "trigger_thread_notes_update",
    # Usage / Cost Calculator Agent
    "THREAD_USAGE_DATA",
    "USAGE_LOCKS",
    "USAGE_STORE_FILE",
    "load_thread_usage_data",
    "save_thread_usage_data",
    "record_thread_tokens",
    "calculate_thread_costs",
    "format_usage_slack_message",
    "update_usage_for_thread",
    "trigger_usage_update",
]
