"""
agents package - Special purpose background and support agents for CaiLogics Slack Platform.
"""

from .thread_notes_agent import (
    THREAD_LOCKS as NOTES_THREAD_LOCKS,
)
from .thread_notes_agent import (
    THREAD_NOTE_MSG_MAP,
    clear_thread_note,
    generate_thread_notes,
    get_thread_note,
    set_thread_note,
    trigger_thread_notes_update,
    update_notes_for_thread,
)
from .thread_notes_agent import (
    get_thread_lock as get_notes_thread_lock,
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

# Exports: Thread Notes / Scribe Agent and Usage / Cost Calculator Agent
__all__ = [
    "NOTES_THREAD_LOCKS",
    "THREAD_NOTE_MSG_MAP",
    "THREAD_USAGE_DATA",
    "USAGE_LOCKS",
    "USAGE_STORE_FILE",
    "calculate_thread_costs",
    "clear_thread_note",
    "format_usage_slack_message",
    "generate_thread_notes",
    "get_notes_thread_lock",
    "get_thread_note",
    "load_thread_usage_data",
    "record_thread_tokens",
    "save_thread_usage_data",
    "set_thread_note",
    "trigger_thread_notes_update",
    "trigger_usage_update",
    "update_notes_for_thread",
    "update_usage_for_thread",
]
