import asyncio
import json
import logging
import time
from typing import Any

from core.database import get_connection, get_db_transaction

logger = logging.getLogger(__name__)


class DiscussionStateStore:
    """Manages active discussion tasks, remaining agent queues, custom flow tags, and stop requests per Slack thread using pure SQLite state."""

    def __init__(self) -> None:
        self.active_thread_tasks: dict[tuple[str, str], asyncio.Task] = {}

    def _get_row(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status, remaining_agents, custom_flow, stop_requested, rounds, round_sequence, initial_prompt, "
                    "active_prompt, is_pure_skill_task, workflow_snapshot, open_debate, consensus_reached, convergence_speakers, "
                    "created_at, last_activity "
                    "FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[DISCUSSION STORE] Failed to read session from DB for ({channel_id}, {thread_ts}): {e}")
            return None

    def _update_fields(self, channel_id: str, thread_ts: str, **updates: Any) -> None:
        now = time.time()
        try:
            with get_db_transaction(immediate=True) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status, remaining_agents, custom_flow, stop_requested, rounds, round_sequence, "
                    "initial_prompt, active_prompt, is_pure_skill_task, workflow_snapshot, open_debate, "
                    "consensus_reached, convergence_speakers, created_at "
                    "FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                data: dict[str, Any] = dict(row) if row else {
                    "status": "active",
                    "remaining_agents": None,
                    "custom_flow": 0,
                    "stop_requested": 0,
                    "rounds": 0,
                    "round_sequence": "[]",
                    "initial_prompt": None,
                    "active_prompt": None,
                    "is_pure_skill_task": 0,
                    "workflow_snapshot": None,
                    "open_debate": 0,
                    "consensus_reached": 0,
                    "convergence_speakers": "[]",
                    "created_at": now,
                }
                for k, v in updates.items():
                    if k in data:
                        data[k] = v

                # Check if session has any active data
                rem_agents_val = data["remaining_agents"]
                has_data = (
                    rem_agents_val is not None
                    or data["custom_flow"] == 1
                    or data["stop_requested"] == 1
                    or data["rounds"] > 0
                    or data["round_sequence"] not in (None, "[]", "")
                    or data["initial_prompt"] is not None
                    or data["active_prompt"] is not None
                    or data["is_pure_skill_task"] == 1
                    or data["workflow_snapshot"] is not None
                    or data["open_debate"] == 1
                    or data["consensus_reached"] == 1
                    or data["convergence_speakers"] not in (None, "[]", "")
                )

                if not has_data:
                    conn.execute(
                        "DELETE FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                        (channel_id, thread_ts),
                    )
                else:
                    rounds_count = data["rounds"]
                    is_active = (rounds_count > 0) or (rem_agents_val is not None and rem_agents_val != "[]")
                    status = "active" if is_active else "completed"
                    conn.execute(
                        """
                        INSERT INTO thread_sessions (
                            channel_id, thread_ts, status, remaining_agents, custom_flow,
                            stop_requested, rounds, round_sequence, initial_prompt,
                            active_prompt, is_pure_skill_task, workflow_snapshot, open_debate,
                            consensus_reached, convergence_speakers, created_at, last_activity
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                            status = excluded.status,
                            remaining_agents = excluded.remaining_agents,
                            custom_flow = excluded.custom_flow,
                            stop_requested = excluded.stop_requested,
                            rounds = excluded.rounds,
                            round_sequence = excluded.round_sequence,
                            initial_prompt = COALESCE(excluded.initial_prompt, thread_sessions.initial_prompt),
                            active_prompt = excluded.active_prompt,
                            is_pure_skill_task = excluded.is_pure_skill_task,
                            workflow_snapshot = excluded.workflow_snapshot,
                            open_debate = excluded.open_debate,
                            consensus_reached = excluded.consensus_reached,
                            convergence_speakers = excluded.convergence_speakers,
                            last_activity = excluded.last_activity;
                        """,
                        (
                            channel_id,
                            thread_ts,
                            status,
                            data["remaining_agents"],
                            data["custom_flow"],
                            data["stop_requested"],
                            data["rounds"],
                            data["round_sequence"],
                            data["initial_prompt"],
                            data["active_prompt"],
                            data["is_pure_skill_task"],
                            data["workflow_snapshot"],
                            data["open_debate"],
                            data["consensus_reached"],
                            data["convergence_speakers"],
                            data["created_at"],
                            now,
                        ),
                    )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"[DISCUSSION STORE] Failed to update session in DB for ({channel_id}, {thread_ts}): {e}",
                exc_info=True,
            )

    def get_remaining_agents(
        self, channel_id: str, thread_ts: str, default: list[str] | None = None
    ) -> list[str]:
        """Returns the list of remaining agents scheduled for this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("remaining_agents") is not None:
            try:
                return list(json.loads(row["remaining_agents"]))
            except (json.JSONDecodeError, TypeError):
                pass
        if default is not None:
            return list(default)
        return []

    def set_remaining_agents(
        self, channel_id: str, thread_ts: str, agents: list[str]
    ) -> None:
        """Sets the remaining agents scheduled for this thread."""
        agents_list = list(agents)
        self._update_fields(channel_id, thread_ts, remaining_agents=json.dumps(agents_list))
        logger.info(
            f"[DISCUSSION STORE] Set remaining agents for {(channel_id, thread_ts)}: {agents_list}"
        )

    def pop_remaining_agent(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> str | None:
        """Pops a specific agent or the next agent from remaining agents list."""
        row = self._get_row(channel_id, thread_ts)
        if not row or row.get("remaining_agents") is None:
            return None
        try:
            agents: list[str] = json.loads(row["remaining_agents"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not agents:
            return None
        if agent_key is None:
            popped = agents.pop(0)
            self._update_fields(channel_id, thread_ts, remaining_agents=json.dumps(agents))
            return popped
        if agent_key in agents:
            agents.remove(agent_key)
            self._update_fields(channel_id, thread_ts, remaining_agents=json.dumps(agents))
            return agent_key
        return None

    def remove_remaining_agent(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> bool:
        """Removes a specific agent from the remaining agents list and syncs to DB."""
        row = self._get_row(channel_id, thread_ts)
        if not row or row.get("remaining_agents") is None:
            return False
        try:
            agents: list[str] = json.loads(row["remaining_agents"])
        except (json.JSONDecodeError, TypeError):
            return False
        if agent_key in agents:
            agents.remove(agent_key)
            self._update_fields(channel_id, thread_ts, remaining_agents=json.dumps(agents))
            return True
        return False

    def pop_random_remaining_agent(
        self, channel_id: str, thread_ts: str
    ) -> str | None:
        """Pops a random agent from remaining agents list and syncs to DB."""
        row = self._get_row(channel_id, thread_ts)
        if not row or row.get("remaining_agents") is None:
            return None
        try:
            agents: list[str] = json.loads(row["remaining_agents"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not agents:
            return None
        import random

        chosen = random.choice(agents)
        agents.remove(chosen)
        self._update_fields(channel_id, thread_ts, remaining_agents=json.dumps(agents))
        return chosen

    def has_remaining_agents_key(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if remaining_agents is explicitly set (even if empty) for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("remaining_agents") is not None)

    def clear_remaining_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears remaining agents for this thread."""
        self._update_fields(channel_id, thread_ts, remaining_agents=None)

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
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("custom_flow"))

    def set_custom_flow(
        self, channel_id: str, thread_ts: str, is_custom: bool = True
    ) -> None:
        """Enables or disables custom flow flag for this thread."""
        self._update_fields(channel_id, thread_ts, custom_flow=1 if is_custom else 0)

    def clear_custom_flow(self, channel_id: str, thread_ts: str) -> None:
        """Clears custom flow flag for this thread."""
        self._update_fields(channel_id, thread_ts, custom_flow=0)

    def is_open_debate(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if this thread is running an open debate without fixed start/end chains."""
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("open_debate"))

    def set_open_debate(
        self, channel_id: str, thread_ts: str, is_open: bool = True
    ) -> None:
        """Enables or disables open debate mode for this thread."""
        self._update_fields(channel_id, thread_ts, open_debate=1 if is_open else 0)

    def clear_open_debate(self, channel_id: str, thread_ts: str) -> None:
        """Clears open debate flag for this thread."""
        self._update_fields(channel_id, thread_ts, open_debate=0)

    def is_stopped(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if stop has been requested for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("stop_requested"))

    def request_stop(self, channel_id: str, thread_ts: str) -> None:
        """Marks thread for stopping."""
        self._update_fields(channel_id, thread_ts, stop_requested=1)

    def clear_stop(self, channel_id: str, thread_ts: str) -> None:
        """Clears stop flag for this thread."""
        self._update_fields(channel_id, thread_ts, stop_requested=0)

    def get_rounds(self, channel_id: str, thread_ts: str) -> int:
        """Returns the number of remaining discussion rounds for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return int(row["rounds"]) if row and row.get("rounds") is not None else 0

    def set_rounds(self, channel_id: str, thread_ts: str, rounds: int) -> None:
        """Sets the remaining discussion rounds for this thread."""
        r = max(0, int(rounds))
        self._update_fields(channel_id, thread_ts, rounds=r)
        logger.info(f"[DISCUSSION STORE] Set rounds for {(channel_id, thread_ts)}: {r}")

    def decrement_rounds(self, channel_id: str, thread_ts: str) -> int:
        """Decrements the remaining discussion rounds for this thread by 1."""
        curr = self.get_rounds(channel_id, thread_ts)
        new_val = max(0, curr - 1)
        self._update_fields(channel_id, thread_ts, rounds=new_val)
        logger.info(f"[DISCUSSION STORE] Decremented rounds for {(channel_id, thread_ts)}: {new_val}")
        return new_val

    def clear_rounds(self, channel_id: str, thread_ts: str) -> None:
        """Clears remaining discussion rounds for this thread."""
        self._update_fields(channel_id, thread_ts, rounds=0)

    def get_round_sequence(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns the established agent sequence for a round in this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("round_sequence"):
            try:
                return list(json.loads(row["round_sequence"]))
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def set_round_sequence(self, channel_id: str, thread_ts: str, sequence: list[str]) -> None:
        """Saves the established agent sequence for a round in this thread."""
        seq_list = list(sequence)
        self._update_fields(channel_id, thread_ts, round_sequence=json.dumps(seq_list))
        logger.info(f"[DISCUSSION STORE] Set round sequence for {(channel_id, thread_ts)}: {seq_list}")

    def clear_round_sequence(self, channel_id: str, thread_ts: str) -> None:
        """Clears the established round sequence for this thread."""
        self._update_fields(channel_id, thread_ts, round_sequence="[]")

    def get_initial_prompt(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the initial user prompt that started this thread."""
        row = self._get_row(channel_id, thread_ts)
        return str(row["initial_prompt"]) if row and row.get("initial_prompt") else None

    def set_initial_prompt(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        """Sets the initial user prompt that started this thread if not already set."""
        if not prompt:
            return
        existing = self.get_initial_prompt(channel_id, thread_ts)
        if not existing:
            self._update_fields(channel_id, thread_ts, initial_prompt=prompt)
            logger.info(f"[DISCUSSION STORE] Stored initial prompt for {(channel_id, thread_ts)} (length: {len(prompt)})")

    def clear_initial_prompt(self, channel_id: str, thread_ts: str) -> None:
        """Clears the stored initial user prompt for this thread."""
        self._update_fields(channel_id, thread_ts, initial_prompt=None)

    def get_active_prompt(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the active human user prompt for the current round in this thread."""
        row = self._get_row(channel_id, thread_ts)
        return str(row["active_prompt"]) if row and row.get("active_prompt") else None

    def set_active_prompt(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        """Updates the active human user prompt for the current round in this thread."""
        if not prompt or not str(prompt).strip():
            return
        self._update_fields(channel_id, thread_ts, active_prompt=str(prompt).strip())
        logger.info(f"[DISCUSSION STORE] Set active prompt for {(channel_id, thread_ts)} (length: {len(str(prompt).strip())})")

    def clear_active_prompt(self, channel_id: str, thread_ts: str) -> None:
        """Clears the active human user prompt for this thread."""
        self._update_fields(channel_id, thread_ts, active_prompt=None)

    def get_is_pure_skill_task(self, channel_id: str, thread_ts: str) -> bool:
        """Returns whether the current round in this thread is a pure skill task."""
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("is_pure_skill_task"))

    def set_is_pure_skill_task(self, channel_id: str, thread_ts: str, is_pure: bool) -> None:
        """Persists whether the current round in this thread is a pure skill task."""
        self._update_fields(channel_id, thread_ts, is_pure_skill_task=1 if is_pure else 0)

    def clear_is_pure_skill_task(self, channel_id: str, thread_ts: str) -> None:
        """Resets the pure-skill task flag for this thread."""
        self._update_fields(channel_id, thread_ts, is_pure_skill_task=0)

    def is_active(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if there is an active running task or pending remaining agents."""
        thread_key = (channel_id, thread_ts)
        task = self.active_thread_tasks.get(thread_key)
        if task and not task.done():
            return True
        return bool(self.get_remaining_agents(channel_id, thread_ts))

    def set_consensus_reached(self, channel_id: str, thread_ts: str, reached: bool = True) -> None:
        self._update_fields(channel_id, thread_ts, consensus_reached=1 if reached else 0)

    def is_consensus_reached(self, channel_id: str, thread_ts: str) -> bool:
        row = self._get_row(channel_id, thread_ts)
        return bool(row and row.get("consensus_reached"))

    def get_workflow_snapshot(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        """Returns the cached workflow configuration snapshot for this thread, if captured."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("workflow_snapshot"):
            try:
                return json.loads(row["workflow_snapshot"])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def set_workflow_snapshot(self, channel_id: str, thread_ts: str, snapshot: dict[str, Any]) -> None:
        """Captures a frozen workflow configuration snapshot for this thread."""
        snap_dict = dict(snapshot)
        self._update_fields(channel_id, thread_ts, workflow_snapshot=json.dumps(snap_dict))
        logger.info(
            f"[DISCUSSION STORE] Captured workflow snapshot for {(channel_id, thread_ts)}: {snap_dict.get('workflow_id')} ({snap_dict.get('mode')})"
        )

    def clear_workflow_snapshot(self, channel_id: str, thread_ts: str) -> None:
        """Clears the workflow configuration snapshot for this thread."""
        self._update_fields(channel_id, thread_ts, workflow_snapshot=None)

    def record_convergence_speaker(self, channel_id: str, thread_ts: str, agent_key: str) -> None:
        """Records a unique agent key in the convergence speaker streak and persists to SQLite."""
        speakers = self.get_convergence_speakers(channel_id, thread_ts)
        if agent_key and agent_key not in speakers:
            speakers.append(agent_key)
            self._update_fields(channel_id, thread_ts, convergence_speakers=json.dumps(speakers))

    def get_convergence_speakers(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns the list of speakers in the current convergence streak from SQLite."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("convergence_speakers"):
            try:
                return list(json.loads(row["convergence_speakers"]))
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def clear_convergence_speakers(self, channel_id: str, thread_ts: str) -> None:
        """Clears the convergence speaker streak for this thread in memory and SQLite."""
        self._update_fields(channel_id, thread_ts, convergence_speakers="[]")

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges discussion state for this thread in a single atomic operation."""
        self.cancel_active_task(channel_id, thread_ts)
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "DELETE FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[DISCUSSION STORE] Failed to delete session from DB: {e}")

    def reset(self) -> None:
        """Resets all discussion states across all threads."""
        self.active_thread_tasks.clear()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_sessions")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[DISCUSSION STORE] Failed to reset thread_sessions table: {e}")
