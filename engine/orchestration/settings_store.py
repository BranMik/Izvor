"""
Generic Pydantic-backed JSON settings store with atomic writes, thread safety,
and explicit cache invalidation.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Generic, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


def atomic_write_json(
    file_path: Path,
    data: dict[str, Any] | list[Any],
    backup: bool = False,
    backup_file: Path | None = None,
) -> None:
    """
    Atomically writes JSON payload to disk using tempfile + fsync + os.replace.
    Optionally creates or refreshes an emergency backup copy.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    dir_path = file_path.parent
    fd, tmp_path_str = tempfile.mkstemp(
        dir=dir_path,
        prefix=f"{file_path.name}.tmp_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp_path_str, file_path)
                break
            except (PermissionError, OSError) as pe:
                if attempt < 9:
                    time.sleep(0.02 * (attempt + 1))
                else:
                    raise pe
        if backup and backup_file:
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copyfile(file_path, backup_file)
    except Exception:
        if os.path.exists(tmp_path_str):
            try:
                os.remove(tmp_path_str)
            except OSError:
                pass
        raise


class JsonSettingsStore(Generic[T]):
    """
    Thread-safe, atomic-write JSON store backed by Pydantic models.
    Replaces polling stat().st_mtime with explicit write-time cache updates.
    """

    def __init__(
        self,
        settings_file: Path,
        model_cls: Type[T],
        backup_file: Path | None = None,
        default_data: dict[str, Any] | None = None,
    ) -> None:
        self.settings_file = settings_file
        self.backup_file = backup_file
        self.model_cls = model_cls
        self.default_data = default_data or {}
        self._lock = threading.RLock()
        self._cached_model: T | None = None
        self._raw_data: dict[str, Any] = {}

    def load(self, force: bool = False) -> T:
        """Loads and validates settings from disk or backup into the cached model."""
        with self._lock:
            if not force and self._cached_model is not None:
                return self._cached_model

            if not self.settings_file.exists():
                self._raw_data = copy.deepcopy(self.default_data)
                self._cached_model = self.model_cls.model_validate(self.default_data)
                return self._cached_model

            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._raw_data = copy.deepcopy(data)
                self._cached_model = self.model_cls.model_validate(data)
                return self._cached_model
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.error(f"[SETTINGS_STORE] Failed loading {self.settings_file}: {e}")
                if self.backup_file and self.backup_file.exists():
                    try:
                        with open(self.backup_file, "r", encoding="utf-8") as bf:
                            data = json.load(bf)
                        self._raw_data = copy.deepcopy(data)
                        self._cached_model = self.model_cls.model_validate(data)
                        logger.warning(
                            f"[SETTINGS_STORE] Recovered settings from backup: {self.backup_file}"
                        )
                        return self._cached_model
                    except (OSError, json.JSONDecodeError, ValueError) as be:
                        logger.error(
                            f"[SETTINGS_STORE] Failed loading backup {self.backup_file}: {be}"
                        )

                self._raw_data = copy.deepcopy(self.default_data)
                self._cached_model = self.model_cls.model_validate(self.default_data)
                return self._cached_model

    def save(self, payload: dict[str, Any] | T) -> None:
        """Atomically persists settings to disk and immediately updates cache."""
        with self._lock:
            if isinstance(payload, BaseModel):
                validated_model = payload
                raw_dict = payload.model_dump()
            else:
                validated_model = self.model_cls.model_validate(payload)
                raw_dict = copy.deepcopy(payload)

            atomic_write_json(self.settings_file, raw_dict)

            # Update cache in-memory immediately
            self._raw_data = copy.deepcopy(raw_dict)
            self._cached_model = validated_model

    def get_model(self) -> T:
        """Returns the active cached model, loading from disk if not yet loaded."""
        return self.load()

    def get_raw_data(self) -> dict[str, Any]:
        """Returns a deepcopy of the raw settings dictionary."""
        with self._lock:
            if not self._raw_data:
                self.load()
            return copy.deepcopy(self._raw_data)

    def invalidate_cache(self) -> None:
        """Explicitly clears the in-memory cached model and raw dictionary."""
        with self._lock:
            self._cached_model = None
            self._raw_data = {}
