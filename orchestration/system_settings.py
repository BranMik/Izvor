import fnmatch
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "system_settings.json"
SYSTEM_BACKUP_FILE = Path(__file__).parent.parent / "data" / "backups" / "system_settings_backup.json"
BACKUP_INTERVAL_SECONDS = 7 * 86400

DEFAULT_PROTECTED_WRITE_PATHS: list[str] = [
    ".env",
    ".git",
    "data/agent_settings.json",
    "data/app_settings.json",
    "data/system_settings.json",
]


class SystemSettingsManager:
    """
    Single Source of Truth (SSOT) for system-level settings that are changeable ONLY in file (not through UI):
    - max_file_size_mb (float)
    - rag (dict): semantic chunking and LanceDB embedding settings
    - workspace_write_blacklist (list[str])
    """

    _RAG_KEY_ALIASES: dict[str, str] = {
        "set_default_top_k": "default_top_k",
        "top_k": "default_top_k",
    }

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        auto_load: bool = True,
    ):
        self._custom_settings_file: bool = settings_file is not None
        self.default_settings_file = settings_file or SYSTEM_SETTINGS_FILE
        self.default_backup_file = backup_file or SYSTEM_BACKUP_FILE

        self.default_fast_model: str = "gemini/gemini-3.8-flash"
        self.default_reasoning_model: str = "mistral/mistral-large-latest"
        self.max_file_size_mb: float = 50.0
        self.workspace_write_blacklist: list[str] = []

        self.rag_config: dict[str, Any] = {
            "max_chunk_chars": 3500,
            "chunk_overlap_chars": 300,
            "min_chunk_chars": 80,
            "default_top_k": 6,
            "embedding_model": "gemini/gemini-embedding-001",
            "embedding_dim": 3072,
            "embedding_batch_size": 32,
        }

        self._last_loaded_mtime: float = 0.0

        if auto_load:
            self.load()

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

    def _parse_rag_section(self, rag_data: Any) -> None:
        if isinstance(rag_data, dict):
            def _get_val(keys: list[str], default: Any) -> Any:
                for k in keys:
                    if k in rag_data and rag_data[k] is not None:
                        return rag_data[k]
                return default

            self.rag_config = {
                "max_chunk_chars": int(_get_val(["max_chunk_chars", "MAX_CHUNK_CHARS"], 3500)),
                "chunk_overlap_chars": int(_get_val(["chunk_overlap_chars", "CHUNK_OVERLAP_CHARS"], 300)),
                "min_chunk_chars": int(_get_val(["min_chunk_chars", "MIN_CHUNK_CHARS"], 80)),
                "default_top_k": int(_get_val(["default_top_k", "DEFAULT_TOP_K", "set_default_top_k", "SET_DEFAULT_TOP_K"], 6)),
                "embedding_model": str(_get_val(["embedding_model", "EMBEDDING_MODEL"], "gemini/gemini-embedding-001")),
                "embedding_dim": int(_get_val(["embedding_dim", "EMBEDDING_DIM"], 3072)),
                "embedding_batch_size": int(_get_val(["embedding_batch_size", "EMBEDDING_BATCH_SIZE"], 32)),
            }

    def load(self) -> None:
        target_file = self.active_settings_file
        if not target_file.exists():
            return

        try:
            mtime = target_file.stat().st_mtime
            if mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return

            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.default_fast_model = (
                str(data.get("default_fast_model", "gemini/gemini-3.8-flash")).strip()
                or "gemini/gemini-3.8-flash"
            )
            self.default_reasoning_model = (
                str(data.get("default_reasoning_model", "mistral/mistral-large-latest")).strip()
                or "mistral/mistral-large-latest"
            )
            self.max_file_size_mb = float(data.get("max_file_size_mb", 50.0))
            self._parse_rag_section(data.get("rag"))
            raw_bl = data.get("workspace_write_blacklist", [])
            self.workspace_write_blacklist = (
                [str(p).strip() for p in raw_bl if str(p).strip()]
                if isinstance(raw_bl, list)
                else []
            )

            self._last_loaded_mtime = mtime
            logger.debug(f"[SYSTEM_SETTINGS] Loaded settings from {target_file}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Failed to load {target_file}: {e}")
            if self.default_backup_file.exists():
                try:
                    with open(self.default_backup_file, "r", encoding="utf-8") as bf:
                        data = json.load(bf)
                    self.default_fast_model = (
                        str(data.get("default_fast_model", "gemini/gemini-3.8-flash")).strip()
                        or "gemini/gemini-3.8-flash"
                    )
                    self.default_reasoning_model = (
                        str(data.get("default_reasoning_model", "mistral/mistral-large-latest")).strip()
                        or "mistral/mistral-large-latest"
                    )
                    self.max_file_size_mb = float(data.get("max_file_size_mb", 50.0))
                    self._parse_rag_section(data.get("rag"))
                    raw_bl = data.get("workspace_write_blacklist", [])
                    self.workspace_write_blacklist = (
                        [str(p).strip() for p in raw_bl if str(p).strip()]
                        if isinstance(raw_bl, list)
                        else []
                    )
                    logger.warning("[SYSTEM_SETTINGS] Recovered settings from backup.")
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as be:
                    logger.error(f"[SYSTEM_SETTINGS] Failed to read backup: {be}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "default_fast_model": self.default_fast_model,
            "default_reasoning_model": self.default_reasoning_model,
            "max_file_size_mb": self.max_file_size_mb,
            "rag": self.rag_config,
            "_comment_workspace_write_blacklist": (
                "Automatic hardcoded protection blocks writes to .env, .git, data/agent_settings.json, "
                "data/app_settings.json, and data/system_settings.json. Add any additional relative workspace "
                "paths or file patterns here to prevent unauthorized modifications."
            ),
            "workspace_write_blacklist": self.workspace_write_blacklist,
        }

        tmp_file = target_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, target_file)
            self._last_loaded_mtime = target_file.stat().st_mtime

            # Update backup if needed
            self._maybe_update_backup(payload)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Error saving settings to {target_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as ue:
                    logger.debug(f"[SYSTEM_SETTINGS] Failed to remove temp file: {ue}")

    def _maybe_update_backup(self, payload: dict[str, Any]) -> None:
        try:
            now = time.time()
            backup = self.default_backup_file
            if not backup.exists() or (now - backup.stat().st_mtime > BACKUP_INTERVAL_SECONDS):
                backup.parent.mkdir(parents=True, exist_ok=True)
                tmp_b = backup.with_suffix(".tmp")
                with open(tmp_b, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp_b, backup)
                logger.info("[SYSTEM_SETTINGS] Updated emergency backup.")
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"[SYSTEM_SETTINGS] Backup update failed: {e}")

    # --- Default Model Settings ---
    def get_default_fast_model(self) -> str:
        self.load()
        return str(self.default_fast_model)

    def get_default_reasoning_model(self) -> str:
        self.load()
        return str(self.default_reasoning_model)

    def set_default_fast_model(self, model: str, save: bool = True) -> None:
        clean = str(model).strip()
        if clean:
            self.default_fast_model = clean
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set default_fast_model to '{self.default_fast_model}'.")

    def set_default_reasoning_model(self, model: str, save: bool = True) -> None:
        clean = str(model).strip()
        if clean:
            self.default_reasoning_model = clean
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set default_reasoning_model to '{self.default_reasoning_model}'.")

    # --- File Size Limit ---
    def get_max_file_size_mb(self) -> float:
        self.load()
        return float(self.max_file_size_mb)

    def get_max_file_size_bytes(self) -> int:
        return int(self.get_max_file_size_mb() * 1024 * 1024)

    def set_max_file_size_mb(self, val: float, save: bool = True) -> None:
        try:
            self.max_file_size_mb = max(0.1, float(val))
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set max_file_size_mb to {self.max_file_size_mb} MB.")
        except (ValueError, TypeError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Invalid max_file_size_mb '{val}': {e}")

    # --- RAG Settings ---
    def get_rag_config(self) -> dict[str, Any]:
        self.load()
        return dict(self.rag_config)

    def get_rag_setting(self, key: str, default: Any = None) -> Any:
        self.load()
        key_lower = key.lower()
        resolved_key = self._RAG_KEY_ALIASES.get(key_lower, key_lower)
        for k, v in self.rag_config.items():
            if k.lower() == resolved_key:
                return v
        return default

    def set_rag_setting(self, key: str, value: Any, save: bool = True) -> None:
        self.load()
        self.rag_config[key] = value
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set RAG setting '{key}' to {value}.")

    # --- Workspace Write Blacklist & Protections ---
    def get_workspace_write_blacklist(self) -> list[str]:
        self.load()
        return list(self.workspace_write_blacklist)

    def set_workspace_write_blacklist(self, paths: list[str], save: bool = True) -> None:
        self.workspace_write_blacklist = [str(p).strip() for p in paths if str(p).strip()]
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set workspace_write_blacklist to {self.workspace_write_blacklist}.")

    def is_path_write_blocked(
        self,
        target_path: str | Path,
        base_dir: Path | None = None,
        custom_blacklist: list[str] | None = None,
    ) -> bool:
        """
        Determines whether writing to target_path is blocked by default protections
        (.env, .git, data/agent_settings.json, data/app_settings.json, data/system_settings.json) or
        the configured workspace_write_blacklist.
        """
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        else:
            base_dir = Path(base_dir).resolve()

        if isinstance(target_path, str):
            clean_str = target_path.strip().replace("\\", "/")
            p_obj = Path(clean_str)
            target = p_obj if p_obj.is_absolute() else (base_dir / clean_str).resolve()
        else:
            target = target_path.resolve()

        try:
            rel_posix = target.relative_to(base_dir).as_posix()
        except ValueError:
            rel_posix = target.as_posix()

        target_name = target.name.lower()

        # 1. Hardcoded default protections:
        # - .env or any .env.* file (e.g. .env, .env.local)
        if target_name == ".env" or target_name.startswith(".env."):
            return True

        # - .git directory or files inside .git
        parts_lower = [part.lower() for part in target.parts]
        if ".git" in parts_lower:
            return True

        # - agent_settings.json, app_settings.json, and system_settings.json
        if target_name in ("agent_settings.json", "app_settings.json", "system_settings.json"):
            return True

        for d_path in DEFAULT_PROTECTED_WRITE_PATHS:
            try:
                d_resolved = (base_dir / d_path).resolve()
                if target == d_resolved or target.is_relative_to(d_resolved):
                    return True
            except (ValueError, OSError):
                pass

        # 2. Configurable workspace_write_blacklist
        blacklist = (
            custom_blacklist
            if custom_blacklist is not None
            else self.get_workspace_write_blacklist()
        )
        for item in blacklist:
            clean_item = str(item).strip()
            if not clean_item:
                continue
            norm_item = clean_item.replace("\\", "/")
            if fnmatch.fnmatch(rel_posix, norm_item) or fnmatch.fnmatch(
                rel_posix, f"*/{norm_item}"
            ):
                return True
            if fnmatch.fnmatch(target_name, norm_item.lower()):
                return True
            try:
                item_resolved = (base_dir / clean_item).resolve()
                if target == item_resolved or target.is_relative_to(item_resolved):
                    return True
            except (ValueError, OSError):
                pass

        return False


system_settings_manager = SystemSettingsManager()


# Module-level convenience functions
def get_max_file_size_mb() -> float:
    return system_settings_manager.get_max_file_size_mb()


def get_max_file_size_bytes() -> int:
    return system_settings_manager.get_max_file_size_bytes()


def set_max_file_size_mb(val: float, save: bool = True) -> None:
    system_settings_manager.set_max_file_size_mb(val, save=save)


def get_rag_config() -> dict[str, Any]:
    return system_settings_manager.get_rag_config()


def get_rag_setting(key: str, default: Any = None) -> Any:
    return system_settings_manager.get_rag_setting(key, default)


def set_rag_setting(key: str, value: Any, save: bool = True) -> None:
    system_settings_manager.set_rag_setting(key, value, save=save)


def get_workspace_write_blacklist() -> list[str]:
    return system_settings_manager.get_workspace_write_blacklist()


def set_workspace_write_blacklist(paths: list[str], save: bool = True) -> None:
    system_settings_manager.set_workspace_write_blacklist(paths, save=save)


def is_path_write_blocked(
    target_path: str | Path,
    base_dir: Path | None = None,
    custom_blacklist: list[str] | None = None,
) -> bool:
    return system_settings_manager.is_path_write_blocked(
        target_path, base_dir=base_dir, custom_blacklist=custom_blacklist
    )


def get_default_fast_model() -> str:
    return system_settings_manager.get_default_fast_model()


def set_default_fast_model(model: str, save: bool = True) -> None:
    system_settings_manager.set_default_fast_model(model, save=save)


def get_default_reasoning_model() -> str:
    return system_settings_manager.get_default_reasoning_model()


def set_default_reasoning_model(model: str, save: bool = True) -> None:
    system_settings_manager.set_default_reasoning_model(model, save=save)


