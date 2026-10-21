import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from core.database import get_connection, get_db_transaction
from core.paths import THREAD_SKILLS_FILE
from orchestration.app_settings import app_settings_manager

logger = logging.getLogger(__name__)


class ThreadSkillsManager:
    """
    Manages runtime thread-scoped global skill grants and expiration.
    Persists thread skills to SQLite thread_skills table with ACID guarantees.
    Reads TTL dynamically from app_settings_manager.get_thread_skill_expiration_time().
    """

    def __init__(
        self,
        storage_file: Path | None = None,
        auto_load: bool = True,
    ):
        self.storage_file = storage_file or THREAD_SKILLS_FILE
        self._threads: dict[str, dict[str, Any]] = {}
        self._last_loaded_mtime: float = 0.0
        self._last_cleanup_time: float = 0.0

        if auto_load:
            self.load()

    def _thread_key(self, channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def migrate_legacy_json_if_present(self) -> None:
        """
        One-time migration: imports active thread skill grants from legacy thread_skills.json
        into SQLite thread_skills table and renames legacy file to thread_skills.json.migrated.
        """
        if not self.storage_file.exists():
            return

        try:
            raw_text = self.storage_file.read_text(encoding="utf-8")
            if not raw_text.strip() or raw_text.strip() == "{}":
                return
            data = json.loads(raw_text)
            threads = data.get("threads", {})
            if not threads:
                return

            now = time.time()
            ttl = self.get_ttl_seconds()

            with get_db_transaction(immediate=True) as conn:
                for key, entry in threads.items():
                    if ":" not in key or not isinstance(entry, dict):
                        continue
                    channel_id, thread_ts = key.split(":", 1)

                    # Global thread skills
                    granted_at = float(entry.get("granted_at", 0.0))
                    skills = entry.get("skills", [])
                    if skills and now - granted_at <= ttl + 86400.0:
                        exp_ts = granted_at + ttl
                        for s in skills:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                                VALUES (?, ?, '*', ?, ?, ?);
                                """,
                                (channel_id, thread_ts, s, granted_at, exp_ts),
                            )

                    # Agent specific skills
                    agent_skills = entry.get("agent_skills", {})
                    for ag_id, ag_entry in agent_skills.items():
                        ag_granted = float(ag_entry.get("granted_at", 0.0))
                        ag_s_list = ag_entry.get("skills", [])
                        if ag_s_list and now - ag_granted <= ttl + 86400.0:
                            ag_exp = ag_granted + ttl
                            for s in ag_s_list:
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                                    VALUES (?, ?, ?, ?, ?, ?);
                                    """,
                                    (channel_id, thread_ts, ag_id, s, ag_granted, ag_exp),
                                )

            migrated_path = self.storage_file.with_suffix(".json.migrated")
            try:
                self.storage_file.replace(migrated_path)
                logger.info(
                    f"[THREAD_SKILLS] Successfully migrated legacy {self.storage_file.name} to SQLite and renamed to {migrated_path.name}"
                )
            except OSError:
                pass
        except Exception as e:
            logger.debug(f"[THREAD_SKILLS] Error migrating legacy thread skills JSON: {e}")

    def load(self) -> None:
        if self.storage_file == THREAD_SKILLS_FILE:
            self.migrate_legacy_json_if_present()
            self._maybe_run_cleanup()
            return

        if not self.storage_file.exists():
            self._threads = {}
            return

        try:
            mtime = self.storage_file.stat().st_mtime
            if mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                self._maybe_run_cleanup()
                return

            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._threads = data.get("threads", {})
            self._last_loaded_mtime = mtime
            logger.debug(f"[THREAD_SKILLS] Loaded {len(self._threads)} thread entries from {self.storage_file}")
            self._maybe_run_cleanup()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"[THREAD_SKILLS] Failed to load {self.storage_file}: {e}")

    def _maybe_run_cleanup(self) -> None:
        now = time.time()
        # Run cleanup at most once per hour (3600 seconds)
        if now - self._last_cleanup_time > 3600.0:
            self._last_cleanup_time = now
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        now = time.time()
        ttl = self.get_ttl_seconds()

        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                with get_db_transaction(immediate=True) as conn:
                    # Clean up rows whose expiration plus 24h grace period has passed
                    conn.execute("DELETE FROM thread_skills WHERE expires_at + 86400.0 < ?;", (now,))
            except Exception as e:
                logger.error(f"[THREAD_SKILLS] SQLite _cleanup_expired failed: {e}")

        if not self._threads:
            return

        modified = False
        threads_to_delete = []

        for key, entry in self._threads.items():
            # Clean up global thread skills. Add 24h grace period so app can still report them as "expired" rather than "never existed".
            if "granted_at" in entry and now - entry["granted_at"] > ttl + 86400.0:
                entry.pop("skills", None)
                entry.pop("granted_at", None)
                modified = True

            # Clean up specific agent skills
            agent_skills = entry.get("agent_skills", {})
            agents_to_delete = []
            for ag_key, ag_entry in agent_skills.items():
                if "granted_at" in ag_entry and now - ag_entry["granted_at"] > ttl + 86400.0:
                    agents_to_delete.append(ag_key)

            for ag_key in agents_to_delete:
                agent_skills.pop(ag_key)
                modified = True

            if "agent_skills" in entry and not agent_skills:
                entry.pop("agent_skills")
                modified = True

            # If the entry is now empty, mark for deletion
            if not entry:
                threads_to_delete.append(key)

        for key in threads_to_delete:
            self._threads.pop(key)
            modified = True

        if modified:
            logger.info(f"[THREAD_SKILLS] Cleaned up {len(threads_to_delete)} expired thread entries. Saving to disk.")
            self.save()

    def save(self) -> None:
        """
        Persists thread skills to disk. For production runtime (THREAD_SKILLS_FILE),
        SQLite is the sole authoritative storage, eliminating runtime file fsync.
        """
        if self.storage_file == THREAD_SKILLS_FILE:
            return

        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.storage_file.with_suffix(".tmp")
        payload = {"threads": self._threads}

        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.storage_file)
            self._last_loaded_mtime = self.storage_file.stat().st_mtime
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[THREAD_SKILLS] Error saving to {self.storage_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as ue:
                    logger.debug(f"[THREAD_SKILLS] Failed to remove temp file: {ue}")

    def get_ttl_seconds(self) -> float:
        ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
        return max(0.1, ttl_hours) * 3600.0

    def grant_skills(
        self, channel_id: str, thread_ts: str, skills: list[str]
    ) -> tuple[list[str], float]:
        """
        Grants a list of skills to all agents in this thread.
        Resets expiration timer from current timestamp.
        Returns (list_of_active_skills, expiration_timestamp).
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        clean_skills = [s.strip() for s in skills if isinstance(s, str) and s.strip()]
        if not clean_skills:
            return self.get_active_skills(channel_id, thread_ts), 0.0

        now = time.time()
        existing_entry = self._threads.get(key, {})
        existing_skills = existing_entry.get("skills", [])

        # Merge deduplicated preserving order
        merged_skills: list[str] = list(existing_skills)
        for s in clean_skills:
            if s not in merged_skills:
                merged_skills.append(s)

        existing_entry["skills"] = merged_skills
        existing_entry["granted_at"] = now
        self._threads[key] = existing_entry
        self.save()

        ttl = self.get_ttl_seconds()
        exp_ts = now + ttl

        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                with get_db_transaction(immediate=True) as conn:
                    for s in merged_skills:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                            VALUES (?, ?, '*', ?, ?, ?);
                            """,
                            (channel_id, thread_ts, s, now, exp_ts),
                        )
            except Exception as e:
                logger.error(f"[THREAD_SKILLS] SQLite grant_skills failed: {e}")

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
        except Exception as ae:
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        logger.info(
            f"[THREAD_SKILLS] Granted skills {clean_skills} to thread {key}. "
            f"Active: {merged_skills}, expires in {ttl / 3600:.1f}h"
        )
        return merged_skills, exp_ts

    def grant_agent_skills(
        self, channel_id: str, thread_ts: str, skill: str, agents: list[str]
    ) -> tuple[dict[str, list[str]], float]:
        """
        Grants a skill to specific agents in this thread.
        Resets expiration timer for each granted agent from current timestamp.
        Returns (dict_of_agent_active_skills, expiration_timestamp).
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        clean_skill = skill.strip() if isinstance(skill, str) else ""
        if not clean_skill or not agents:
            return {}, 0.0

        now = time.time()
        existing_entry = self._threads.get(key, {})
        agent_skills = existing_entry.setdefault("agent_skills", {})

        result: dict[str, list[str]] = {}
        for ag in agents:
            ag_clean = ag.strip()
            if not ag_clean:
                continue
            ag_entry = agent_skills.get(ag_clean, {})
            current_skills = list(ag_entry.get("skills", []))
            if clean_skill not in current_skills:
                current_skills.append(clean_skill)
            agent_skills[ag_clean] = {
                "skills": current_skills,
                "granted_at": now,
            }
            result[ag_clean] = current_skills

        self._threads[key] = existing_entry
        self.save()

        ttl = self.get_ttl_seconds()
        exp_ts = now + ttl

        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                with get_db_transaction(immediate=True) as conn:
                    for ag_clean in result:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at)
                            VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            (channel_id, thread_ts, ag_clean, clean_skill, now, exp_ts),
                        )
            except Exception as e:
                logger.error(f"[THREAD_SKILLS] SQLite grant_agent_skills failed: {e}")

        try:
            from services.audit_logger import log_audit_event, AuditActor

            log_audit_event(
                event_type="THREAD_SKILL_GRANTED",
                actor=AuditActor(type="system", id="thread_skills_manager"),
                action="grant_agent_skills",
                target=f"{channel_id}:{thread_ts}",
                details={
                    "skill": clean_skill,
                    "agents": list(result.keys()),
                    "ttl_hours": ttl / 3600.0,
                    "expires_at": exp_ts,
                },
            )
        except Exception as ae:
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        logger.info(
            f"[THREAD_SKILLS] Granted skill '{clean_skill}' to agents {list(result.keys())} in thread {key}. "
            f"Expires in {ttl / 3600:.1f}h"
        )
        return result, exp_ts

    def get_active_agent_skills(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> list[str]:
        """
        Returns list of skills currently active (unexpired) for a specific agent in this thread.
        Authoritative lookup from SQLite thread_skills with in-memory JSON fallback.
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
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
                    if rows is not None and len(rows) > 0:
                        return [r["skill_name"] for r in rows]
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_active_agent_skills fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        agent_skills = entry.get("agent_skills", {})
        ag_entry = agent_skills.get(agent_key)
        if not ag_entry:
            return []

        granted_at = ag_entry.get("granted_at", 0.0)
        ttl = self.get_ttl_seconds()

        if now - granted_at <= ttl:
            return list(ag_entry.get("skills", []))
        return []

    def get_all_active_agent_skills(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, list[str]]:
        """
        Returns mapping of {agent_key: [active_skills]} for all agents with active grants in this thread.
        Authoritative lookup from SQLite thread_skills with in-memory JSON fallback.
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
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
                    if rows is not None and len(rows) > 0:
                        active_map: dict[str, list[str]] = {}
                        for r in rows:
                            active_map.setdefault(r["agent_id"], []).append(r["skill_name"])
                        return active_map
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_all_active_agent_skills fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return {}

        agent_skills = entry.get("agent_skills", {})
        ttl = self.get_ttl_seconds()
        active_map: dict[str, list[str]] = {}

        for ag_key, ag_entry in agent_skills.items():
            granted_at = ag_entry.get("granted_at", 0.0)
            if now - granted_at <= ttl:
                skills = list(ag_entry.get("skills", []))
                if skills:
                    active_map[ag_key] = skills

        return active_map

    def get_expired_agent_skills(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> list[str]:
        """
        Returns list of expired agent skills in this thread (for specific agent or all agents).
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        agent_skills = entry.get("agent_skills", {})
        now = time.time()
        ttl = self.get_ttl_seconds()
        expired: list[str] = []

        if agent_key is not None:
            ag_entry = agent_skills.get(agent_key)
            if ag_entry and now - ag_entry.get("granted_at", 0.0) > ttl:
                return list(ag_entry.get("skills", []))
            return []

        for ag_entry in agent_skills.values():
            granted_at = ag_entry.get("granted_at", 0.0)
            if now - granted_at > ttl:
                for s in ag_entry.get("skills", []):
                    if s not in expired:
                        expired.append(s)

        return expired

    def revoke_skills(
        self,
        channel_id: str,
        thread_ts: str,
        skills: list[str] | None = None,
        agent_key: str | None = None,
    ) -> list[str]:
        """
        Revokes specific skills or all skills if skills is None or contains 'all'.
        If agent_key is specified, revokes only from that agent.
        Returns remaining active skills.
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)

        # SQLite delete sync
        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                with get_db_transaction(immediate=True) as conn:
                    if agent_key is not None:
                        if not skills or any(s.lower() == "all" for s in skills):
                            conn.execute("DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND agent_id = ?;", (channel_id, thread_ts, agent_key))
                        else:
                            clean_to_remove_list = [s.strip() for s in skills if isinstance(s, str)]
                            placeholders = ",".join("?" for _ in clean_to_remove_list)
                            conn.execute(f"DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND agent_id = ? AND skill_name IN ({placeholders});", [channel_id, thread_ts, agent_key, *clean_to_remove_list])
                    else:
                        if not skills or any(s.lower() == "all" for s in skills):
                            conn.execute("DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ?;", (channel_id, thread_ts))
                        else:
                            clean_to_remove_list = [s.strip() for s in skills if isinstance(s, str)]
                            placeholders = ",".join("?" for _ in clean_to_remove_list)
                            conn.execute(f"DELETE FROM thread_skills WHERE channel_id = ? AND thread_ts = ? AND skill_name IN ({placeholders});", [channel_id, thread_ts, *clean_to_remove_list])
            except Exception as e:
                logger.error(f"[THREAD_SKILLS] SQLite revoke_skills failed: {e}")

        try:
            from services.audit_logger import log_audit_event, AuditActor

            log_audit_event(
                event_type="THREAD_SKILL_REVOKED",
                actor=AuditActor(type="system", id="thread_skills_manager"),
                action="revoke_skills",
                target=f"{channel_id}:{thread_ts}",
                details={
                    "skills": skills,
                    "agent_key": agent_key,
                },
            )
        except Exception as ae:
            logger.debug(f"[THREAD_SKILLS] Audit event emission error: {ae}")

        if key not in self._threads:
            return []

        # If targeting a specific agent
        if agent_key is not None:
            agent_skills = self._threads[key].get("agent_skills", {})
            if agent_key not in agent_skills:
                return []
            if not skills or any(s.lower() == "all" for s in skills):
                agent_skills.pop(agent_key, None)
                self.save()
                return []
            clean_to_remove = {s.strip() for s in skills if isinstance(s, str)}
            current = agent_skills[agent_key].get("skills", [])
            remaining = [s for s in current if s not in clean_to_remove]
            if remaining:
                agent_skills[agent_key]["skills"] = remaining
            else:
                agent_skills.pop(agent_key, None)
            self.save()
            return remaining

        # Global revocation
        if not skills or any(s.lower() == "all" for s in skills):
            self._threads.pop(key, None)
            self.save()
            logger.info(f"[THREAD_SKILLS] Revoked all thread-global skills for {key}")
            return []

        clean_to_remove = {s.strip() for s in skills if isinstance(s, str)}
        current_skills = self._threads[key].get("skills", [])
        remaining = [s for s in current_skills if s not in clean_to_remove]

        if remaining:
            self._threads[key]["skills"] = remaining
        else:
            self._threads[key].pop("skills", None)

        self.save()
        logger.info(f"[THREAD_SKILLS] Revoked {clean_to_remove} from {key}. Remaining: {remaining}")
        return remaining

    def get_active_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        """
        Returns list of skills currently active (unexpired) in the specified thread.
        Authoritative lookup from SQLite thread_skills with in-memory JSON fallback.
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
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
                    if rows is not None and len(rows) > 0:
                        return [r["skill_name"] for r in rows]
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_active_skills fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        granted_at = entry.get("granted_at", 0.0)
        ttl = self.get_ttl_seconds()

        if now - granted_at <= ttl:
            return list(entry.get("skills", []))
        return []

    def get_expired_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        """
        Returns list of skills that were granted to this thread but are now expired.
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
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
                    if rows is not None and len(rows) > 0:
                        return [r["skill_name"] for r in rows]
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_expired_skills fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        granted_at = entry.get("granted_at", 0.0)
        ttl = self.get_ttl_seconds()

        if now - granted_at > ttl:
            return list(entry.get("skills", []))
        return []

    def get_expired_agent_skills(self, channel_id: str, thread_ts: str, agent_key: str) -> list[str]:
        """
        Returns list of skills that were granted to a specific agent in this thread but are now expired.
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                with get_connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT skill_name FROM thread_skills
                        WHERE channel_id = ? AND thread_ts = ? AND agent_id = ? AND expires_at <= ?
                        ORDER BY rowid ASC;
                        """,
                        (channel_id, thread_ts, agent_key, now),
                    ).fetchall()
                    if rows is not None and len(rows) > 0:
                        return [r["skill_name"] for r in rows]
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_expired_agent_skills fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        agent_skills = entry.get("agent_skills", {})
        ag_entry = agent_skills.get(agent_key)
        if not ag_entry:
            return []

        granted_at = ag_entry.get("granted_at", 0.0)
        ttl = self.get_ttl_seconds()

        if now - granted_at > ttl:
            return list(ag_entry.get("skills", []))
        return []

    def get_expiration_status(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> dict[str, Any]:
        """
        Returns status dictionary for thread skills:
        {"has_skills": bool, "active": bool, "skills": list, "seconds_remaining": float, "granted_at": float}
        """
        now = time.time()
        if self.storage_file == THREAD_SKILLS_FILE:
            try:
                target_agent = agent_key if agent_key is not None else "*"
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
            except Exception as e:
                logger.debug(f"[THREAD_SKILLS] SQLite get_expiration_status fallback: {e}")

        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return {"has_skills": False, "active": False, "skills": [], "seconds_remaining": 0.0, "granted_at": 0.0}

        if agent_key is not None:
            agent_skills = entry.get("agent_skills", {})
            ag_entry = agent_skills.get(agent_key)
            if not ag_entry:
                return {"has_skills": False, "active": False, "skills": [], "seconds_remaining": 0.0, "granted_at": 0.0}
            granted_at = ag_entry.get("granted_at", 0.0)
            skills = list(ag_entry.get("skills", []))
        else:
            granted_at = entry.get("granted_at", 0.0)
            skills = list(entry.get("skills", []))

        ttl = self.get_ttl_seconds()
        remaining = max(0.0, (granted_at + ttl) - now)
        is_active = (now - granted_at <= ttl)

        return {
            "has_skills": bool(skills),
            "active": is_active,
            "skills": skills,
            "seconds_remaining": remaining,
            "granted_at": granted_at,
        }


thread_skills_manager = ThreadSkillsManager()
