import json
import logging
from typing import Any

from core.database import get_connection, get_db_transaction
from core.models import FileContext, HistoryLimitConfig

logger = logging.getLogger(__name__)


def _safe_json(obj: Any) -> str:
    def _def(o: Any) -> Any:
        if isinstance(o, (set, frozenset)):
            return list(o)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    return json.dumps(obj, default=_def)


class ThreadContextStore:
    """Manages thread-level contextual constraints (history limits, word limits, language locks, and file attachments) using pure SQLite state."""

    def __init__(self) -> None:
        pass

    def _get_row(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
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
                return dict(row) if row else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to read context from DB for ({channel_id}, {thread_ts}): {e}")
            return None

    def _update_fields(self, channel_id: str, thread_ts: str, **updates: Any) -> None:
        try:
            with get_db_transaction(immediate=True) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT history_limit_config, max_words, min_words, scribe_max_words, scribe_min_words, "
                    "language, excluded_agents, included_agents, format_modifiers, file_context "
                    "FROM thread_context WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                data: dict[str, Any] = dict(row) if row else {
                    "history_limit_config": None,
                    "max_words": None,
                    "min_words": None,
                    "scribe_max_words": None,
                    "scribe_min_words": None,
                    "language": None,
                    "excluded_agents": "[]",
                    "included_agents": "[]",
                    "format_modifiers": "{}",
                    "file_context": None,
                }
                for k, v in updates.items():
                    if k in data:
                        data[k] = v

                # Check if completely empty
                has_data = any([
                    data["history_limit_config"] is not None,
                    data["max_words"] is not None,
                    data["min_words"] is not None,
                    data["scribe_max_words"] is not None,
                    data["scribe_min_words"] is not None,
                    data["language"] is not None,
                    data["excluded_agents"] not in (None, "[]", ""),
                    data["included_agents"] not in (None, "[]", ""),
                    data["format_modifiers"] not in (None, "{}", ""),
                    data["file_context"] is not None,
                ])

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
                            file_context = excluded.file_context;
                        """,
                        (
                            channel_id,
                            thread_ts,
                            data["history_limit_config"],
                            data["max_words"],
                            data["min_words"],
                            data["scribe_max_words"],
                            data["scribe_min_words"],
                            data["language"],
                            data["excluded_agents"],
                            data["included_agents"],
                            data["format_modifiers"],
                            data["file_context"],
                        ),
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to update context in DB for ({channel_id}, {thread_ts}): {e}")

    def get_history_limit(self, channel_id: str, thread_ts: str) -> dict[str, int] | None:
        """Returns active history limit dictionary for this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("history_limit_config"):
            try:
                return json.loads(row["history_limit_config"])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def set_history_limit(self, channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int) -> None:
        """Sets history limit configuration for this thread."""
        cfg = HistoryLimitConfig(
            limit=limit,
            prior_cutoff_index=prior_cutoff_index,
        ).to_dict()
        self._update_fields(channel_id, thread_ts, history_limit_config=json.dumps(cfg))
        logger.info(
            f"[CONTEXT STORE] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
        )

    def clear_history_limit(self, channel_id: str, thread_ts: str) -> None:
        """Clears active history limit for this thread."""
        self._update_fields(channel_id, thread_ts, history_limit_config=None)

    def get_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns max_words limit for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return int(row["max_words"]) if row and row.get("max_words") is not None else None

    def set_max_words(self, channel_id: str, thread_ts: str, max_words: int) -> None:
        """Sets max_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, max_words=int(max_words))
        logger.info(f"[CONTEXT STORE] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}")

    def clear_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears max_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, max_words=None)

    def get_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns min_words limit for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return int(row["min_words"]) if row and row.get("min_words") is not None else None

    def set_min_words(self, channel_id: str, thread_ts: str, min_words: int) -> None:
        """Sets min_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, min_words=int(min_words))
        logger.info(f"[CONTEXT STORE] Set min_words limit for thread {(channel_id, thread_ts)}: min_words={min_words}")

    def clear_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears min_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, min_words=None)

    def get_scribe_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_max_words limit for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return int(row["scribe_max_words"]) if row and row.get("scribe_max_words") is not None else None

    def set_scribe_max_words(self, channel_id: str, thread_ts: str, max_words: int) -> None:
        """Sets scribe_max_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, scribe_max_words=int(max_words))
        logger.info(f"[CONTEXT STORE] Set scribe_max_words limit for thread {(channel_id, thread_ts)}: {max_words}")

    def clear_scribe_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_max_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, scribe_max_words=None)

    def get_scribe_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_min_words limit for this thread."""
        row = self._get_row(channel_id, thread_ts)
        return int(row["scribe_min_words"]) if row and row.get("scribe_min_words") is not None else None

    def set_scribe_min_words(self, channel_id: str, thread_ts: str, min_words: int) -> None:
        """Sets scribe_min_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, scribe_min_words=int(min_words))
        logger.info(f"[CONTEXT STORE] Set scribe_min_words limit for thread {(channel_id, thread_ts)}: {min_words}")

    def clear_scribe_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_min_words limit for this thread."""
        self._update_fields(channel_id, thread_ts, scribe_min_words=None)

    def get_language(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the locked language for this thread, if any."""
        row = self._get_row(channel_id, thread_ts)
        return str(row["language"]) if row and row.get("language") else None

    def set_language(self, channel_id: str, thread_ts: str, language: str) -> None:
        """Sets and locks the language for this thread."""
        self._update_fields(channel_id, thread_ts, language=str(language))
        logger.info(f"[CONTEXT STORE] Set locked language for thread {(channel_id, thread_ts)}: {language}")

    def clear_language(self, channel_id: str, thread_ts: str) -> None:
        """Clears locked language for this thread."""
        self._update_fields(channel_id, thread_ts, language=None)

    def get_excluded_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents persistently excluded from debate in this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("excluded_agents"):
            try:
                return list(json.loads(row["excluded_agents"]))
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def set_excluded_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets persistently excluded agents for this thread."""
        cleaned = list(dict.fromkeys(agents))
        self._update_fields(channel_id, thread_ts, excluded_agents=json.dumps(cleaned))
        logger.info(f"[CONTEXT STORE] Set excluded agents for thread {(channel_id, thread_ts)}: {agents}")

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
            self._update_fields(channel_id, thread_ts, excluded_agents=json.dumps(curr))

    def clear_excluded_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all excluded agents for this thread."""
        self._update_fields(channel_id, thread_ts, excluded_agents="[]")

    def get_included_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents explicitly forced into debate in this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("included_agents"):
            try:
                return list(json.loads(row["included_agents"]))
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def set_included_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets explicitly included agents for this thread."""
        cleaned = list(dict.fromkeys(agents))
        # Ensure included agents are removed from excluded list
        curr_excluded = self.get_excluded_agents(channel_id, thread_ts)
        remaining_excluded = [a for a in curr_excluded if a not in agents]
        self._update_fields(
            channel_id,
            thread_ts,
            included_agents=json.dumps(cleaned),
            excluded_agents=json.dumps(remaining_excluded),
        )
        logger.info(f"[CONTEXT STORE] Set included agents for thread {(channel_id, thread_ts)}: {agents}")

    def add_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Appends an agent to the persistent included list for this thread."""
        curr = self.get_included_agents(channel_id, thread_ts)
        if agent not in curr:
            curr.append(agent)
            self.set_included_agents(channel_id, thread_ts, curr)
        else:
            self.remove_excluded_agent(channel_id, thread_ts, agent)

    def remove_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Removes an agent from the persistent included list for this thread."""
        curr = self.get_included_agents(channel_id, thread_ts)
        if agent in curr:
            curr = [a for a in curr if a != agent]
            self._update_fields(channel_id, thread_ts, included_agents=json.dumps(curr))

    def clear_included_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all explicitly included agents for this thread."""
        self._update_fields(channel_id, thread_ts, included_agents="[]")

    def get_format_modifiers(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        """Returns custom format modifiers dictionary for this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("format_modifiers"):
            try:
                val = json.loads(row["format_modifiers"])
                if isinstance(val, dict):
                    return dict(val)
                if isinstance(val, (list, tuple, set)):
                    return {item: True for item in val if item}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def set_format_modifiers(self, channel_id: str, thread_ts: str, modifiers: Any) -> None:
        """Sets custom format modifiers for this thread."""
        if isinstance(modifiers, dict):
            fmt = dict(modifiers)
        elif isinstance(modifiers, (list, tuple, set)):
            fmt = {item: True for item in modifiers if item}
        elif modifiers:
            fmt = {str(modifiers): True}
        else:
            fmt = {}
        self._update_fields(channel_id, thread_ts, format_modifiers=json.dumps(fmt))

    def clear_format_modifiers(self, channel_id: str, thread_ts: str) -> None:
        """Clears custom format modifiers for this thread."""
        self._update_fields(channel_id, thread_ts, format_modifiers="{}")

    def get_file_context(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        """Returns active file context dictionary for this thread."""
        row = self._get_row(channel_id, thread_ts)
        if row and row.get("file_context"):
            try:
                fc_data = json.loads(row["file_context"])
                if isinstance(fc_data, dict) and isinstance(fc_data.get("authorized_agents"), list):
                    fc_data["authorized_agents"] = set(fc_data["authorized_agents"])
                return fc_data
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def set_file_context(self, channel_id: str, thread_ts: str, file_context: dict[str, Any] | FileContext) -> None:
        """Sets active file context for this thread."""
        ctx_dict = file_context.to_dict() if isinstance(file_context, FileContext) else file_context
        self._update_fields(channel_id, thread_ts, file_context=_safe_json(ctx_dict))
        logger.info(
            f"[CONTEXT STORE] Set active file context for thread {(channel_id, thread_ts)}: file='{ctx_dict.get('file_name')}', authorized_agents={ctx_dict.get('authorized_agents')}"
        )

    def clear_file_context(self, channel_id: str, thread_ts: str) -> None:
        """Clears active file context for this thread."""
        self._update_fields(channel_id, thread_ts, file_context=None)

    def revoke_all(self, channel_id: str, thread_ts: str) -> None:
        """Revokes all format constraints, language locks, and agent exclusions/inclusions for this thread."""
        self._update_fields(
            channel_id,
            thread_ts,
            max_words=None,
            min_words=None,
            scribe_max_words=None,
            scribe_min_words=None,
            excluded_agents="[]",
            included_agents="[]",
            format_modifiers="{}",
            language=None,
        )
        logger.info(f"[CONTEXT STORE] Revoked all constraints for thread {(channel_id, thread_ts)}")

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges context limits and data for this thread in a single atomic transaction."""
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
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_context")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CONTEXT STORE] Failed to reset thread_context table: {e}")
