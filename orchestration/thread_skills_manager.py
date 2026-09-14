import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from orchestration.app_settings import app_settings_manager

logger = logging.getLogger(__name__)

THREAD_SKILLS_FILE = Path(__file__).parent.parent / "data" / "thread_skills.json"


class ThreadSkillsManager:
    """
    Manages runtime thread-scoped global skill grants and expiration.
    Persists thread skills to data/thread_skills.json with atomic writes.
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

        if auto_load:
            self.load()

    def _thread_key(self, channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def load(self) -> None:
        if not self.storage_file.exists():
            self._threads = {}
            return

        try:
            mtime = self.storage_file.stat().st_mtime
            if mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return

            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._threads = data.get("threads", {})
            self._last_loaded_mtime = mtime
            logger.debug(f"[THREAD_SKILLS] Loaded {len(self._threads)} thread entries from {self.storage_file}")
        except Exception as e:
            logger.error(f"[THREAD_SKILLS] Failed to load {self.storage_file}: {e}")

    def save(self) -> None:
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
        except Exception as e:
            logger.error(f"[THREAD_SKILLS] Error saving to {self.storage_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

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

        self._threads[key] = {
            "skills": merged_skills,
            "granted_at": now,
        }
        self.save()

        ttl = self.get_ttl_seconds()
        exp_ts = now + ttl
        logger.info(
            f"[THREAD_SKILLS] Granted skills {clean_skills} to thread {key}. "
            f"Active: {merged_skills}, expires in {ttl / 3600:.1f}h"
        )
        return merged_skills, exp_ts

    def revoke_skills(
        self, channel_id: str, thread_ts: str, skills: list[str] | None = None
    ) -> list[str]:
        """
        Revokes specific skills or all skills if skills is None or contains 'all'.
        Returns remaining active skills.
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        if key not in self._threads:
            return []

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
            self._threads.pop(key, None)

        self.save()
        logger.info(f"[THREAD_SKILLS] Revoked {clean_to_remove} from {key}. Remaining: {remaining}")
        return remaining

    def get_active_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        """
        Returns list of skills currently active (unexpired) in the specified thread.
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        granted_at = entry.get("granted_at", 0.0)
        now = time.time()
        ttl = self.get_ttl_seconds()

        if now - granted_at <= ttl:
            return list(entry.get("skills", []))
        return []

    def get_expired_skills(self, channel_id: str, thread_ts: str) -> list[str]:
        """
        Returns list of skills that were granted to this thread but are now expired.
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return []

        granted_at = entry.get("granted_at", 0.0)
        now = time.time()
        ttl = self.get_ttl_seconds()

        if now - granted_at > ttl:
            return list(entry.get("skills", []))
        return []

    def get_expiration_status(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        """
        Returns status dictionary for thread skills:
        {"has_skills": bool, "active": bool, "skills": list, "seconds_remaining": float, "granted_at": float}
        """
        self.load()
        key = self._thread_key(channel_id, thread_ts)
        entry = self._threads.get(key)
        if not entry:
            return {"has_skills": False, "active": False, "skills": [], "seconds_remaining": 0.0}

        granted_at = entry.get("granted_at", 0.0)
        now = time.time()
        ttl = self.get_ttl_seconds()
        remaining = max(0.0, (granted_at + ttl) - now)
        is_active = (now - granted_at <= ttl)

        return {
            "has_skills": True,
            "active": is_active,
            "skills": list(entry.get("skills", [])),
            "seconds_remaining": remaining,
            "granted_at": granted_at,
        }


thread_skills_manager = ThreadSkillsManager()
