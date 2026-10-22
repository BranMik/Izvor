"""
Pure SQLite Repository for thread-scoped skill grants and expiration.
Eliminates obsolete JSON file sync and dual-backend branching.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.database import get_connection, get_db_transaction
from orchestration.app_settings import app_settings_manager

logger = logging.getLogger(__name__)


class ThreadSkillsManager:
    """
    Manages runtime thread-scoped skill grants and expiration.
    Authoritative SQLite thread_skills table with ACID transactions.
    Reads TTL dynamically from app_settings_manager.get_thread_skill_expiration_time().
    """

    def __init__(self, *args: Any, auto_load: bool = True, **kwargs: Any) -> None:
        self._last_cleanup_time: float = 0.0
        if auto_load:
            self._maybe_run_cleanup()

    def _maybe_run_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup_time > 3600.0:
            self._last_cleanup_time = now
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        now = time.time()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_skills WHERE expires_at + 86400.0 < ?;", (now,))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite _cleanup_expired failed: {e}")

    def load(self) -> None:
        self._maybe_run_cleanup()

    def save(self) -> None:
        pass

    def get_ttl_seconds(self) -> float:
        ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
        return max(0.1, ttl_hours) * 3600.0

    def grant_skills(
        self, channel_id: str, thread_ts: str, skills: list[str]
    ) -> tuple[list[str], float]:
        clean_skills = [s.strip() for s in skills if isinstance(s, str) and s.strip()]
        if not clean_skills:
            return self.get_active_skills(channel_id, thread_ts), 0.0

        now = time.time()
        ttl = self.get_ttl_seconds()
        exp_ts = now + ttl

        try:
            with get_db_transaction(immediate=True) as conn:
                for s in clean_skills:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                        VALUES (?, ?, '*', ?, ?, ?);
                        """,
                        (channel_id, thread_ts, s, now, exp_ts),
                    )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite grant_skills failed: {e}")

        active = self.get_active_skills(channel_id, thread_ts)
        try:
            from services.audit_logger import log_audit_event, AuditActor

            log_audit_event(
                event_type="THREAD_SKILL_GRANTED",
                actor=AuditActor(type="system", id="thread_skills_manager"),
                action="grant_skills",
                target=f"{channel_id}:{thread_ts}",
                details={
                    "skills": clean_skills,
                    "agent_id": "*",
                    "ttl_hours": ttl / 3600.0,
                    "expires_at": exp_ts,
                },
            )
        except Exception as ae:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        return active, exp_ts

    def grant_agent_skills(
        self, channel_id: str, thread_ts: str, skill: str, agents: list[str]
    ) -> tuple[dict[str, list[str]], float]:
        clean_skill = skill.strip() if isinstance(skill, str) else ""
        if not clean_skill or not agents:
            return {}, 0.0

        now = time.time()
        ttl = self.get_ttl_seconds()
        exp_ts = now + ttl

        try:
            with get_db_transaction(immediate=True) as conn:
                for ag in agents:
                    ag_clean = ag.strip()
                    if ag_clean:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                            VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            (channel_id, thread_ts, ag_clean, clean_skill, now, exp_ts),
                        )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite grant_agent_skills failed: {e}")

        active_map = self.get_all_active_agent_skills(channel_id, thread_ts)
        try:
            from services.audit_logger import log_audit_event, AuditActor

            log_audit_event(
                event_type="THREAD_SKILL_GRANTED",
                actor=AuditActor(type="system", id="thread_skills_manager"),
                action="grant_agent_skills",
                target=f"{channel_id}:{thread_ts}",
                details={
                    "skill": clean_skill,
                    "agents": agents,
                    "ttl_hours": ttl / 3600.0,
                    "expires_at": exp_ts,
                },
            )
        except Exception as ae:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        return active_map, exp_ts

    def get_active_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        now = time.time()
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT skill_name FROM thread_skills
                    WHERE channel_id = ? AND thread_ts = ? AND agent_id = '*' AND expires_at > ?
                    ORDER BY rowid ASC;
                    """,
                    (channel_id, thread_ts, now),
                ).fetchall()
                if rows:
                    return [r["skill_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_active_skills error: {e}")
        return []

    def get_expired_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        now = time.time()
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT skill_name FROM thread_skills
                    WHERE channel_id = ? AND thread_ts = ? AND agent_id = '*' AND expires_at <= ?
                    ORDER BY rowid ASC;
                    """,
                    (channel_id, thread_ts, now),
                ).fetchall()
                if rows:
                    return [r["skill_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_expired_skills error: {e}")
        return []

    def get_active_agent_skills(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> list[str]:
        now = time.time()
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT skill_name FROM thread_skills
                    WHERE channel_id = ? AND thread_ts = ? AND agent_id = ? AND expires_at > ?
                    ORDER BY rowid ASC;
                    """,
                    (channel_id, thread_ts, agent_key, now),
                ).fetchall()
                if rows:
                    return [r["skill_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_active_agent_skills error: {e}")
        return []

    def get_all_active_agent_skills(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, list[str]]:
        now = time.time()
        active_map: dict[str, list[str]] = {}
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT agent_id, skill_name FROM thread_skills
                    WHERE channel_id = ? AND thread_ts = ? AND agent_id != '*' AND expires_at > ?
                    ORDER BY rowid ASC;
                    """,
                    (channel_id, thread_ts, now),
                ).fetchall()
                for r in rows:
                    active_map.setdefault(r["agent_id"], []).append(r["skill_name"])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_all_active_agent_skills error: {e}")
        return active_map

    def get_expired_agent_skills(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> list[str]:
        now = time.time()
        try:
            with get_connection() as conn:
                if agent_key is not None:
                    rows = conn.execute(
                        """
                        SELECT skill_name FROM thread_skills
                        WHERE channel_id = ? AND thread_ts = ? AND agent_id = ? AND expires_at <= ?
                        ORDER BY rowid ASC;
                        """,
                        (channel_id, thread_ts, agent_key, now),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT skill_name FROM thread_skills
                        WHERE channel_id = ? AND thread_ts = ? AND agent_id != '*' AND expires_at <= ?
                        ORDER BY rowid ASC;
                        """,
                        (channel_id, thread_ts, now),
                    ).fetchall()
                return [r["skill_name"] for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_expired_agent_skills error: {e}")
        return []

    def revoke_skills(
        self,
        channel_id: str,
        thread_ts: str,
        skills: list[str] | None = None,
        agent_key: str | None = None,
    ) -> list[str]:
        try:
            with get_db_transaction(immediate=True) as conn:
                if agent_key is not None:
                    if not skills or any(s.lower() == "all" for s in skills):
                        conn.execute(
                            "DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND agent_id = ?;",
                            (channel_id, thread_ts, agent_key),
                        )
                    else:
                        clean = [s.strip() for s in skills if isinstance(s, str)]
                        placeholders = ",".join("?" for _ in clean)
                        conn.execute(
                            f"DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND agent_id = ? AND skill_name IN ({placeholders});",
                            [channel_id, thread_ts, agent_key, *clean],
                        )
                else:
                    if not skills or any(s.lower() == "all" for s in skills):
                        conn.execute(
                            "DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ?;",
                            (channel_id, thread_ts),
                        )
                    else:
                        clean = [s.strip() for s in skills if isinstance(s, str)]
                        placeholders = ",".join("?" for _ in clean)
                        conn.execute(
                            f"DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND skill_name IN ({placeholders});",
                            [channel_id, thread_ts, *clean],
                        )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite revoke_skills failed: {e}")

        try:
            from services.audit_logger import log_audit_event, AuditActor

            log_audit_event(
                event_type="THREAD_SKILL_REVOKED",
                actor=AuditActor(type="system", id="thread_skills_manager"),
                action="revoke_skills",
                target=f"{channel_id}:{thread_ts}",
                details={"skills": skills, "agent_key": agent_key},
            )
        except Exception as ae:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        if agent_key is not None:
            return self.get_active_agent_skills(channel_id, thread_ts, agent_key)
        return self.get_active_skills(channel_id, thread_ts)

    def get_expiration_status(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> dict[str, Any]:
        now = time.time()
        target_agent = agent_key if agent_key is not None else "*"
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT skill_name, granted_at, expires_at FROM thread_skills
                    WHERE channel_id = ? AND thread_ts = ? AND agent_id = ?
                    ORDER BY expires_at DESC;
                    """,
                    (channel_id, thread_ts, target_agent),
                ).fetchall()
                if rows:
                    active_skills = [r["skill_name"] for r in rows if r["expires_at"] > now]
                    all_skills = [r["skill_name"] for r in rows]
                    max_expires_at = rows[0]["expires_at"]
                    min_granted_at = min(r["granted_at"] for r in rows)
                    remaining = max(0.0, max_expires_at - now)
                    is_active = len(active_skills) > 0
                    return {
                        "has_skills": len(all_skills) > 0,
                        "active": is_active,
                        "skills": active_skills if is_active else all_skills,
                        "seconds_remaining": remaining,
                        "granted_at": min_granted_at,
                    }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD_SKILLS] SQLite get_expiration_status error: {e}")

        return {
            "has_skills": False,
            "active": False,
            "skills": [],
            "seconds_remaining": 0.0,
            "granted_at": 0.0,
        }

    def clear_all(self) -> None:
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_skills;")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite clear_all error: {e}")

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ?;",
                    (channel_id, thread_ts),
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[THREAD_SKILLS] SQLite clear_thread error: {e}")


thread_skills_manager = ThreadSkillsManager()
