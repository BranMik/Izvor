import json
import logging
from typing import Any

from core.database import get_connection, get_db_transaction
from core.models import FileContext, HistoryLimitConfig

logger = logging.getLogger(__name__)


class ThreadContextStore:
    """Manages thread-level contextual constraints (history limits, word limits, language locks, and file attachments)."""

    def __init__(self) -> None:
        self.thread_history_limits: dict[tuple[str, str], dict[str, int]] = {}
        self.thread_max_words_limits: dict[tuple[str, str], int] = {}
        self.thread_min_words_limits: dict[tuple[str, str], int] = {}
        self.thread_scribe_max_words_limits: dict[tuple[str, str], int] = {}
        self.thread_scribe_min_words_limits: dict[tuple[str, str], int] = {}
        self.thread_languages: dict[tuple[str, str], str] = {}
        self.thread_excluded_agents: dict[tuple[str, str], list[str]] = {}
        self.thread_included_agents: dict[tuple[str, str], list[str]] = {}
        self.thread_format_modifiers: dict[tuple[str, str], dict[str, Any]] = {}
        self.thread_active_file_context: dict[tuple[str, str], dict[str, Any]] = {}
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
                    "SELECT history_limit_config, max_words, min_words, scribe_max_words, scribe_min_words, "
                    "language, excluded_agents, included_agents, format_modifiers, file_context "
                    "FROM thread_context WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                if row:
                    if row["history_limit_config"] and key not in self.thread_history_limits:
                        self.thread_history_limits[key] = json.loads(row["history_limit_config"])
                    if row["max_words"] is not None and key not in self.thread_max_words_limits:
                        self.thread_max_words_limits[key] = row["max_words"]
                    if row["min_words"] is not None and key not in self.thread_min_words_limits:
                        self.thread_min_words_limits[key] = row["min_words"]
                    if row["scribe_max_words"] is not None and key not in self.thread_scribe_max_words_limits:
                        self.thread_scribe_max_words_limits[key] = row["scribe_max_words"]
                    if row["scribe_min_words"] is not None and key not in self.thread_scribe_min_words_limits:
                        self.thread_scribe_min_words_limits[key] = row["scribe_min_words"]
                    if row["language"] and key not in self.thread_languages:
                        self.thread_languages[key] = row["language"]
                    if row["excluded_agents"] and key not in self.thread_excluded_agents:
                        excl = json.loads(row["excluded_agents"])
                        if excl:
                            self.thread_excluded_agents[key] = excl
                    if row["included_agents"] and key not in self.thread_included_agents:
                        incl = json.loads(row["included_agents"])
                        if incl:
                            self.thread_included_agents[key] = incl
                    if row["format_modifiers"] and key not in self.thread_format_modifiers:
                        fmt = json.loads(row["format_modifiers"])
                        if fmt:
                            self.thread_format_modifiers[key] = fmt
                    if row["file_context"] and key not in self.thread_active_file_context:
                        fc_data = json.loads(row["file_context"])
                        if isinstance(fc_data, dict) and isinstance(fc_data.get("authorized_agents"), list):
                            fc_data["authorized_agents"] = set(fc_data["authorized_agents"])
                        self.thread_active_file_context[key] = fc_data
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"[CONTEXT STORE] Failed to load context from DB for {key}: {e}")

    def _sync_to_db(self, channel_id: str, thread_ts: str) -> None:
        def _safe_json(obj: Any) -> str:
            def _def(o: Any) -> Any:
                if isinstance(o, (set, frozenset)):
                    return list(o)
                raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
            return json.dumps(obj, default=_def)

        key = (channel_id, thread_ts)
        h_limit = json.dumps(self.thread_history_limits[key]) if key in self.thread_history_limits else None
        max_w = self.thread_max_words_limits.get(key)
        min_w = self.thread_min_words_limits.get(key)
        s_max_w = self.thread_scribe_max_words_limits.get(key)
        s_min_w = self.thread_scribe_min_words_limits.get(key)
        lang = self.thread_languages.get(key)
        excl = json.dumps(self.thread_excluded_agents.get(key, []))
        incl = json.dumps(self.thread_included_agents.get(key, []))
        fmt = json.dumps(self.thread_format_modifiers.get(key, {}))
        file_ctx = _safe_json(self.thread_active_file_context[key]) if key in self.thread_active_file_context else None

        has_data = any([
            h_limit is not None,
            max_w is not None,
            min_w is not None,
            s_max_w is not None,
            s_min_w is not None,
            lang is not None,
            excl != "[]",
            incl != "[]",
            fmt != "{}",
            file_ctx is not None,
        ])

        try:
            with get_db_transaction(immediate=True) as conn:
                if not has_data:
                    conn.execute(
                        "DELETE FROM thread_context WHERE channel_id = ? AND thread_ts = ?",
                        (channel_id, thread_ts),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO thread_context (
                            channel_id, thread_ts, history_limit_config, max_words, min_words,
                            scribe_max_words, scribe_min_words, language, excluded_agents,
                            included_agents, format_modifiers, file_context
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                            history_limit_config = excluded.history_limit_config,
                            max_words = excluded.max_words,
                            min_words = excluded.min_words,
                            scribe_max_words = excluded.scribe_max_words,
                            scribe_min_words = excluded.scribe_min_words,
                            language = excluded.language,
                            excluded_agents = excluded.excluded_agents,
                            included_agents = excluded.included_agents,
                            format_modifiers = excluded.format_modifiers,
                            file_context = excluded.file_context
                        """,
                        (
                            channel_id,
                            thread_ts,
                            h_limit,
                            max_w,
                            min_w,
                            s_max_w,
                            s_min_w,
                            lang,
                            excl,
                            incl,
                            fmt,
                            file_ctx,
                        ),
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to sync context to DB for {key}: {e}")

    def get_history_limit(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, int] | None:
        """Returns active history limit dictionary for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_history_limits.get((channel_id, thread_ts))

    def set_history_limit(
        self, channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int
    ) -> None:
        """Sets history limit configuration for this thread."""
        key = (channel_id, thread_ts)
        self.thread_history_limits[key] = HistoryLimitConfig(
            limit=limit,
            prior_cutoff_index=prior_cutoff_index,
        ).to_dict()
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
        )

    def clear_history_limit(self, channel_id: str, thread_ts: str) -> None:
        """Clears active history limit for this thread."""
        self.thread_history_limits.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns max_words limit for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_max_words_limits.get((channel_id, thread_ts))

    def set_max_words(
        self, channel_id: str, thread_ts: str, max_words: int
    ) -> None:
        """Sets max_words limit for this thread."""
        key = (channel_id, thread_ts)
        self.thread_max_words_limits[key] = max_words
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}"
        )

    def clear_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears max_words limit for this thread."""
        self.thread_max_words_limits.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns min_words limit for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_min_words_limits.get((channel_id, thread_ts))

    def set_min_words(
        self, channel_id: str, thread_ts: str, min_words: int
    ) -> None:
        """Sets min_words limit for this thread."""
        key = (channel_id, thread_ts)
        self.thread_min_words_limits[key] = min_words
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set min_words limit for thread {(channel_id, thread_ts)}: min_words={min_words}"
        )

    def clear_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears min_words limit for this thread."""
        self.thread_min_words_limits.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_scribe_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_max_words limit for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_scribe_max_words_limits.get((channel_id, thread_ts))

    def set_scribe_max_words(
        self, channel_id: str, thread_ts: str, max_words: int
    ) -> None:
        """Sets scribe_max_words limit for this thread."""
        key = (channel_id, thread_ts)
        self.thread_scribe_max_words_limits[key] = max_words
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set scribe_max_words limit for thread {(channel_id, thread_ts)}: {max_words}"
        )

    def clear_scribe_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_max_words limit for this thread."""
        self.thread_scribe_max_words_limits.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_scribe_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_min_words limit for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_scribe_min_words_limits.get((channel_id, thread_ts))

    def set_scribe_min_words(
        self, channel_id: str, thread_ts: str, min_words: int
    ) -> None:
        """Sets scribe_min_words limit for this thread."""
        key = (channel_id, thread_ts)
        self.thread_scribe_min_words_limits[key] = min_words
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set scribe_min_words limit for thread {(channel_id, thread_ts)}: {min_words}"
        )

    def clear_scribe_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_min_words limit for this thread."""
        self.thread_scribe_min_words_limits.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_language(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the locked language for this thread, if any."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_languages.get((channel_id, thread_ts))

    def set_language(self, channel_id: str, thread_ts: str, language: str) -> None:
        """Sets and locks the language for this thread."""
        key = (channel_id, thread_ts)
        self.thread_languages[key] = language
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set locked language for thread {(channel_id, thread_ts)}: {language}"
        )

    def clear_language(self, channel_id: str, thread_ts: str) -> None:
        """Clears locked language for this thread."""
        self.thread_languages.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_excluded_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents persistently excluded from debate in this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return list(self.thread_excluded_agents.get((channel_id, thread_ts), []))

    def set_excluded_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets persistently excluded agents for this thread."""
        key = (channel_id, thread_ts)
        self.thread_excluded_agents[key] = list(dict.fromkeys(agents))
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set excluded agents for thread {(channel_id, thread_ts)}: {agents}"
        )

    def add_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Appends an agent to the persistent excluded list for this thread."""
        curr = self.get_excluded_agents(channel_id, thread_ts)
        if agent not in curr:
            curr.append(agent)
            self.set_excluded_agents(channel_id, thread_ts, curr)

    def remove_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Removes an agent from the persistent excluded list for this thread."""
        curr = self.get_excluded_agents(channel_id, thread_ts)
        if agent in curr:
            curr = [a for a in curr if a != agent]
            if curr:
                self.thread_excluded_agents[(channel_id, thread_ts)] = curr
            else:
                self.thread_excluded_agents.pop((channel_id, thread_ts), None)
            self._sync_to_db(channel_id, thread_ts)

    def clear_excluded_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all excluded agents for this thread."""
        self.thread_excluded_agents.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_included_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents explicitly forced into debate in this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return list(self.thread_included_agents.get((channel_id, thread_ts), []))

    def set_included_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets explicitly included agents for this thread."""
        key = (channel_id, thread_ts)
        self.thread_included_agents[key] = list(dict.fromkeys(agents))
        self._loaded_threads.add(key)
        # Ensure included agents are removed from excluded list
        curr_excluded = self.get_excluded_agents(channel_id, thread_ts)
        remaining_excluded = [a for a in curr_excluded if a not in agents]
        if remaining_excluded != curr_excluded:
            self.thread_excluded_agents[key] = remaining_excluded
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set included agents for thread {(channel_id, thread_ts)}: {agents}"
        )

    def add_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Appends an agent to the persistent included list for this thread."""
        curr = self.get_included_agents(channel_id, thread_ts)
        if agent not in curr:
            curr.append(agent)
            self.set_included_agents(channel_id, thread_ts, curr)
        self.remove_excluded_agent(channel_id, thread_ts, agent)

    def clear_included_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all explicitly included agents for this thread."""
        self.thread_included_agents.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_format_modifiers(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns custom format modifiers list for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        val = self.thread_format_modifiers.get((channel_id, thread_ts))
        if isinstance(val, (list, tuple, set)):
            return list(val)
        if isinstance(val, dict):
            return list(val.keys())
        return []

    def set_format_modifiers(
        self, channel_id: str, thread_ts: str, modifiers: Any
    ) -> None:
        """Sets custom format modifiers for this thread."""
        key = (channel_id, thread_ts)
        if isinstance(modifiers, (list, tuple, set)):
            self.thread_format_modifiers[key] = list(modifiers)
        elif isinstance(modifiers, dict):
            self.thread_format_modifiers[key] = list(modifiers.keys())
        elif modifiers:
            self.thread_format_modifiers[key] = [str(modifiers)]
        else:
            self.thread_format_modifiers[key] = []
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)

    def clear_format_modifiers(self, channel_id: str, thread_ts: str) -> None:
        """Clears custom format modifiers for this thread."""
        self.thread_format_modifiers.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def get_file_context(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, Any] | None:
        """Returns active file context dictionary for this thread."""
        self._ensure_loaded(channel_id, thread_ts)
        return self.thread_active_file_context.get((channel_id, thread_ts))

    def set_file_context(
        self, channel_id: str, thread_ts: str, file_context: dict[str, Any] | FileContext
    ) -> None:
        """Sets active file context for this thread."""
        ctx_dict = (
            file_context.to_dict()
            if isinstance(file_context, FileContext)
            else file_context
        )
        key = (channel_id, thread_ts)
        self.thread_active_file_context[key] = ctx_dict
        self._loaded_threads.add(key)
        self._sync_to_db(channel_id, thread_ts)
        logger.info(
            f"[CONTEXT STORE] Set active file context for thread {(channel_id, thread_ts)}: file='{ctx_dict.get('file_name')}', authorized_agents={ctx_dict.get('authorized_agents')}"
        )

    def clear_file_context(self, channel_id: str, thread_ts: str) -> None:
        """Clears active file context for this thread."""
        self.thread_active_file_context.pop((channel_id, thread_ts), None)
        self._sync_to_db(channel_id, thread_ts)

    def revoke_all(self, channel_id: str, thread_ts: str) -> None:
        """Revokes all format constraints, language locks, and agent exclusions/inclusions for this thread."""
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_scribe_max_words(channel_id, thread_ts)
        self.clear_scribe_min_words(channel_id, thread_ts)
        self.clear_excluded_agents(channel_id, thread_ts)
        self.clear_included_agents(channel_id, thread_ts)
        self.clear_format_modifiers(channel_id, thread_ts)
        self.clear_language(channel_id, thread_ts)
        logger.info(f"[CONTEXT STORE] Revoked all constraints for thread {(channel_id, thread_ts)}")

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges context limits and data for this thread."""
        self.clear_history_limit(channel_id, thread_ts)
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_scribe_max_words(channel_id, thread_ts)
        self.clear_scribe_min_words(channel_id, thread_ts)
        self.clear_language(channel_id, thread_ts)
        self.clear_excluded_agents(channel_id, thread_ts)
        self.clear_included_agents(channel_id, thread_ts)
        self.clear_format_modifiers(channel_id, thread_ts)
        self.clear_file_context(channel_id, thread_ts)
        self._loaded_threads.discard((channel_id, thread_ts))
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "DELETE FROM thread_context WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to delete context from DB: {e}")

    def reset(self) -> None:
        """Resets all context limits and state across all threads."""
        self.thread_history_limits.clear()
        self.thread_max_words_limits.clear()
        self.thread_min_words_limits.clear()
        self.thread_scribe_max_words_limits.clear()
        self.thread_scribe_min_words_limits.clear()
        self.thread_languages.clear()
        self.thread_excluded_agents.clear()
        self.thread_included_agents.clear()
        self.thread_format_modifiers.clear()
        self.thread_active_file_context.clear()
        self._loaded_threads.clear()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_context")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to reset thread_context table: {e}")
