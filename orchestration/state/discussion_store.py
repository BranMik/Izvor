import asyncio
import json
import logging
import time
from typing import Any

from core.database import get_connection, get_db_transaction

logger = logging.getLogger(__name__)


class DiscussionStateStore:
    """Manages active discussion tasks, remaining agent queues, custom flow tags, and stop requests per Slack thread."""

    def __init__(self) -> None:
        self.remaining_agents_map: dict[tuple[str, str], list[str]] = {}
        self.active_thread_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.custom_flow_threads: set[tuple[str, str]] = set()
        self.open_debate_threads: set[tuple[str, str]] = set()
        self.stop_requested_threads: set[tuple[str, str]] = set()
        self.thread_rounds: dict[tuple[str, str], int] = {}
        self.thread_round_sequences: dict[tuple[str, str], list[str]] = {}
        self.thread_initial_prompts: dict[tuple[str, str], str] = {}
        self._loaded_threads: set[tuple[str, str]] = set()

    def _ensure_loaded(self, channel_id: str, thread_ts: str) -> None:
        key = (channel_id, thread_ts)
        if key in self._loaded_threads:
            return
        self._loaded_threads.add(key)
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT remaining_agents, custom_flow, stop_requested, rounds, round_sequence, initial_prompt "
                    "FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                if row:
                    if key not in self.remaining_agents_map:
                        if row["remaining_agents"] is not None:
                            self.remaining_agents_map[key] = json.loads(row["remaining_agents"])
                    if row["custom_flow"] and key not in self.custom_flow_threads:
                        self.custom_flow_threads.add(key)
                    if row["stop_requested"] and key not in self.stop_requested_threads:
                        self.stop_requested_threads.add(key)
                    if row["rounds"] and key not in self.thread_rounds:
                        self.thread_rounds[key] = row["rounds"]
                    if key not in self.thread_round_sequences:
                        seq = json.loads(row["round_sequence"])
                        if seq:
                            self.thread_round_sequences[key] = seq
                    if row["initial_prompt"] and key not in self.thread_initial_prompts:
                        self.thread_initial_prompts[key] = row["initial_prompt"]
        except Exception as e:
            logger.debug(f"[DISCUSSION STORE] Failed to load session from DB for {key}: {e}")

    def _sync_to_db(self, channel_id: str, thread_ts: str) -> None:
        key = (channel_id, thread_ts)
        rem_agents = self.remaining_agents_map.get(key)
        custom = 1 if key in self.custom_flow_threads else 0
        stopped = 1 if key in self.stop_requested_threads else 0
        rounds = self.thread_rounds.get(key, 0)
        seq = self.thread_round_sequences.get(key)
        prompt = self.thread_initial_prompts.get(key)

        has_data = (
            rem_agents is not None
            or custom == 1
            or stopped == 1
            or rounds > 0
            or seq is not None
            or prompt is not None
        )

        try:
            with get_db_transaction(immediate=True) as conn:
                if not has_data:
                    conn.execute(
                        "DELETE FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                        (channel_id, thread_ts),
                    )
                else:
                    now = time.time()
                    status = "active" if (rounds > 0 or (rem_agents and len(rem_agents) > 0)) else "completed"
                    conn.execute(
                        """
                        INSERT INTO thread_sessions (
                            channel_id, thread_ts, status, remaining_agents, custom_flow,
                            stop_requested, rounds, round_sequence, initial_prompt, created_at, last_activity
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                            status = excluded.status,
                            remaining_agents = excluded.remaining_agents,
                            custom_flow = excluded.custom_flow,
                            stop_requested = excluded.stop_requested,
                            rounds = excluded.rounds,
                            round_sequence = excluded.round_sequence,
                            initial_prompt = COALESCE(excluded.initial_prompt, thread_sessions.initial_prompt),
                            last_activity = excluded.last_activity
                        """,
                        (
                            channel_id,
                            thread_ts,
                            status,
                            json.dumps(rem_agents or []),
                            custom,
                            stopped,
                            rounds,
                            json.dumps(seq or []),
                            prompt,
                            now,
                            now,
                        ),
                    )
        except Exception as e:
            logger.debug(f"[DISCUSSION STORE] Failed to sync session to DB for {key}: {e}")

    def get_remaining_agents(
        self, channel_id: str, thread_ts: str, default: list[str] | None = None
    ) -> list[str]:
        """Returns the list of remaining agents scheduled for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        if thread_key in self.remaining_agents_map:
            return list(self.remaining_agents_map[thread_key])
        if default is not None:
            return list(default)
        return []

    def set_remaining_agents(
        self, channel_id: str, thread_ts: str, agents: list[str]
    ) -> None:
        """Sets the remaining agents scheduled for this thread."""
        self.remaining_agents_map[(channel_id, thread_ts)] = list(agents)
        self._loaded_threads.add((channel_id, thread_ts))
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[DISCUSSION STORE] Set remaining agents for {(channel_id, thread_ts)}: {agents}"
        )

    def pop_remaining_agent(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> str | None:
        """Pops a specific agent or the next agent from remaining agents list."""
        self._ensure_loaded(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        agents = self.remaining_agents_map.get(thread_key, [])
        if not agents:
            return None
        if agent_key is None:
            popped = agents.pop(0)
            self._sync_to_db(channel_id, thread_ts)
            return popped
        if agent_key in agents:
            agents.remove(agent_key)
            self._sync_to_db(channel_id, thread_ts)
            return agent_key
    def remove_remaining_agent(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> bool:
        """Removes a specific agent from the remaining agents list and syncs to DB."""
        self._ensure_loaded(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        agents = self.remaining_agents_map.get(thread_key, [])
        if agent_key in agents:
            agents.remove(agent_key)
            self._sync_to_db(channel_id, thread_ts)
            return True
        return False

    def pop_random_remaining_agent(
        self, channel_id: str, thread_ts: str
    ) -> str | None:
        """Pops a random agent from remaining agents list and syncs to DB."""
        self._ensure_loaded(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        agents = self.remaining_agents_map.get(thread_key, [])
        if not agents:
            return None
        import random

        chosen = random.choice(agents)
        agents.remove(chosen)
        self._sync_to_db(channel_id, thread_ts)
        return chosen


    def has_remaining_agents_key(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if remaining_agents_map explicitly has an entry for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return (channel_id, thread_ts) in self.remaining_agents_map

    def clear_remaining_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears remaining agents for this thread."""
        self.remaining_agents_map.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_active_task(
        self, channel_id: str, thread_ts: str
    ) -> asyncio.Task | None:
        """Gets active asyncio Task for this thread."""
        return self.active_thread_tasks.get((channel_id, thread_ts))

    def set_active_task(
        self, channel_id: str, thread_ts: str, task: asyncio.Task
    ) -> None:
        """Associates an active asyncio Task with this thread."""
        self.active_thread_tasks[(channel_id, thread_ts)] = task

    def pop_active_task(
        self, channel_id: str, thread_ts: str
    ) -> asyncio.Task | None:
        """Removes and returns active asyncio Task for this thread."""
        return self.active_thread_tasks.pop((channel_id, thread_ts), None)

    def clear_active_task(
        self,
        channel_id: str,
        thread_ts: str,
        task: asyncio.Task | None = None,
    ) -> None:
        """Clears active task for this thread, optionally only if it matches current task."""
        thread_key = (channel_id, thread_ts)
        if task is None or self.active_thread_tasks.get(thread_key) == task:
            self.active_thread_tasks.pop(thread_key, None)

    def cancel_active_task(
        self, channel_id: str, thread_ts: str
    ) -> asyncio.Task | None:
        """Cancels and pops active task if running and not the current asyncio task."""
        thread_key = (channel_id, thread_ts)
        task = self.active_thread_tasks.pop(thread_key, None)
        if task and not task.done() and task is not asyncio.current_task():
            logger.info(
                f"[DISCUSSION STORE] Cancelling active background task for thread {thread_key}"
            )
            task.cancel()
        return task

    def is_custom_flow(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if this thread is running an ad-hoc custom tagged agent flow."""
        self._ensure_loaded(channel_id, thread_ts)
        return (channel_id, thread_ts) in self.custom_flow_threads

    def set_custom_flow(
        self, channel_id: str, thread_ts: str, is_custom: bool = True
    ) -> None:
        """Enables or disables custom flow flag for this thread."""
        thread_key = (channel_id, thread_ts)
        self._loaded_threads.add(thread_key)
        if is_custom:
            self.custom_flow_threads.add(thread_key)
        else:
            self.custom_flow_threads.discard(thread_key)
        self._sync_to_db(channel_id, thread_ts)

    def clear_custom_flow(self, channel_id: str, thread_ts: str) -> None:
        """Clears custom flow flag for this thread."""
        self.custom_flow_threads.discard((channel_id, thread_ts))
        self._sync_to_db(channel_id, thread_ts)

    def is_open_debate(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if this thread is running an open debate without fixed start/end chains."""
        self._ensure_loaded(channel_id, thread_ts)
        return (channel_id, thread_ts) in self.open_debate_threads

    def set_open_debate(
        self, channel_id: str, thread_ts: str, is_open: bool = True
    ) -> None:
        """Enables or disables open debate mode for this thread."""
        thread_key = (channel_id, thread_ts)
        self._loaded_threads.add(thread_key)
        if is_open:
            self.open_debate_threads.add(thread_key)
        else:
            self.open_debate_threads.discard(thread_key)

    def clear_open_debate(self, channel_id: str, thread_ts: str) -> None:
        """Clears open debate flag for this thread."""
        self.open_debate_threads.discard((channel_id, thread_ts))

    def is_stopped(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if stop has been requested for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return (channel_id, thread_ts) in self.stop_requested_threads

    def request_stop(self, channel_id: str, thread_ts: str) -> None:
        """Marks thread for stopping."""
        self.stop_requested_threads.add((channel_id, thread_ts))
        self._loaded_threads.add((channel_id, thread_ts))
        self._sync_to_db(channel_id, thread_ts)

    def clear_stop(self, channel_id: str, thread_ts: str) -> None:
        """Clears stop flag for this thread."""
        self.stop_requested_threads.discard((channel_id, thread_ts))
        self._sync_to_db(channel_id, thread_ts)

    def get_rounds(self, channel_id: str, thread_ts: str) -> int:
        """Returns the number of remaining discussion rounds for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_rounds.get((channel_id, thread_ts), 0)

    def set_rounds(self, channel_id: str, thread_ts: str, rounds: int) -> None:
        """Sets the remaining discussion rounds for this thread."""
        thread_key = (channel_id, thread_ts)
        self._loaded_threads.add(thread_key)
        self.thread_rounds[thread_key] = max(0, int(rounds))
        self._sync_to_db(channel_id, thread_ts)
        logger.info(f"[DISCUSSION STORE] Set rounds for {thread_key}: {self.thread_rounds[thread_key]}")

    def decrement_rounds(self, channel_id: str, thread_ts: str) -> int:
        """Decrements the remaining discussion rounds for this thread by 1."""
        self._ensure_loaded(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        current = self.thread_rounds.get(thread_key, 0)
        new_val = max(0, current - 1)
        self.thread_rounds[thread_key] = new_val
        self._sync_to_db(channel_id, thread_ts)
        logger.info(f"[DISCUSSION STORE] Decremented rounds for {thread_key}: {new_val}")
        return new_val

    def clear_rounds(self, channel_id: str, thread_ts: str) -> None:
        """Clears remaining discussion rounds for this thread."""
        self.thread_rounds.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_round_sequence(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns the established agent sequence for a round in this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return list(self.thread_round_sequences.get((channel_id, thread_ts), []))

    def set_round_sequence(self, channel_id: str, thread_ts: str, sequence: list[str]) -> None:
        """Saves the established agent sequence for a round in this thread."""
        thread_key = (channel_id, thread_ts)
        self._loaded_threads.add(thread_key)
        self.thread_round_sequences[thread_key] = list(sequence)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(f"[DISCUSSION STORE] Set round sequence for {(channel_id, thread_ts)}: {sequence}")

    def clear_round_sequence(self, channel_id: str, thread_ts: str) -> None:
        """Clears the established round sequence for this thread."""
        self.thread_round_sequences.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_initial_prompt(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the initial user prompt that started this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_initial_prompts.get((channel_id, thread_ts))

    def set_initial_prompt(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        """Sets the initial user prompt that started this thread if not already set."""
        self._ensure_loaded(channel_id, thread_ts)
        key = (channel_id, thread_ts)
        if key not in self.thread_initial_prompts and prompt:
            self.thread_initial_prompts[key] = prompt
            self._sync_to_db(channel_id, thread_ts)
            logger.info(f"[DISCUSSION STORE] Stored initial prompt for {key} (length: {len(prompt)})")

    def clear_initial_prompt(self, channel_id: str, thread_ts: str) -> None:
        """Clears the stored initial user prompt for this thread."""
        self.thread_initial_prompts.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def is_active(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if there is an active running task or pending remaining agents."""
        thread_key = (channel_id, thread_ts)
        task = self.active_thread_tasks.get(thread_key)
        if task and not task.done():
            return True
        return bool(self.get_remaining_agents(channel_id, thread_ts))

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges discussion state for this thread."""
        self.clear_remaining_agents(channel_id, thread_ts)
        self.clear_custom_flow(channel_id, thread_ts)
        self.clear_open_debate(channel_id, thread_ts)
        self.clear_stop(channel_id, thread_ts)
        self.cancel_active_task(channel_id, thread_ts)
        self.clear_rounds(channel_id, thread_ts)
        self.clear_round_sequence(channel_id, thread_ts)
        self.clear_initial_prompt(channel_id, thread_ts)
        self._loaded_threads.discard((channel_id, thread_ts))
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "DELETE FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
        except Exception as e:
            logger.debug(f"[DISCUSSION STORE] Failed to delete session from DB: {e}")

    def reset(self) -> None:
        """Resets all discussion states across all threads."""
        self.remaining_agents_map.clear()
        self.active_thread_tasks.clear()
        self.custom_flow_threads.clear()
        self.open_debate_threads.clear()
        self.stop_requested_threads.clear()
        self.thread_rounds.clear()
        self.thread_round_sequences.clear()
        self.thread_initial_prompts.clear()
        self._loaded_threads.clear()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_sessions")
        except Exception as e:
            logger.debug(f"[DISCUSSION STORE] Failed to reset thread_sessions table: {e}")
