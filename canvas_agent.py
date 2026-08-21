"""
canvas_agent.py - Compatibility re-exporter for thread_notes_agent.
All active live-note functionality now lives in thread_notes_agent.py.
"""
from thread_notes_agent import (  # noqa: F401
    THREAD_LOCKS,
    THREAD_NOTE_MSG_MAP,
    _PENDING_UPDATES,
    _RUNNING_TASKS,
    clear_thread_note,
    generate_thread_notes,
    get_thread_lock,
    get_thread_note,
    set_thread_note,
    trigger_thread_notes_update,
    update_notes_for_thread,
)

# Compatibility aliases
THREAD_CANVAS_MAP = THREAD_NOTE_MSG_MAP
get_thread_canvas = get_thread_note
set_thread_canvas = set_thread_note
clear_thread_canvas = clear_thread_note
generate_canvas_notes = generate_thread_notes
update_canvas_for_thread = update_notes_for_thread
trigger_canvas_update = trigger_thread_notes_update
