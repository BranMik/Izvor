"""
core/paths.py — Canonical Filesystem Path Definitions for Izvor.

Provides centralized path constants with zero internal application imports
(only standard library `os` and `pathlib.Path`) to prevent circular dependency
deadlocks across core, orchestration, services, and agent layers.
"""

from __future__ import annotations

import os
from pathlib import Path

# Engine and Project Root anchors
ENGINE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = ENGINE_DIR.parent


def _bootstrap_dotenv(env_file: Path) -> None:
    """
    Lightweight, standard-library-only .env reader to populate os.environ before
    path resolution. Ensures IZVOR_* overrides in .env take effect immediately
    without introducing third-party dependencies or circular imports.
    """
    if not env_file.is_file():
        return
    try:
        with open(env_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("\"'")
                    if key:
                        os.environ.setdefault(key, val)
    except OSError:
        pass


_bootstrap_dotenv(PROJECT_ROOT / ".env")


def _resolve_env_path(env_var: str, default: Path) -> Path:
    """
    Resolves an environment variable override to a Path.
    If the override path is relative, it is anchored to PROJECT_ROOT rather than process CWD.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


# Three Primary Pillars
CONFIG_DIR = _resolve_env_path("IZVOR_CONFIG_DIR", PROJECT_ROOT / "config")
DATA_DIR = _resolve_env_path("IZVOR_DATA_DIR", PROJECT_ROOT / "data")

# 1. config/ Pillar (Implementer Configuration)
SETTINGS_DIR = _resolve_env_path("IZVOR_SETTINGS_DIR", CONFIG_DIR / "settings")
SYSTEM_SETTINGS_FILE = SETTINGS_DIR / "system_settings.json"
AGENT_SETTINGS_FILE = SETTINGS_DIR / "agent_settings.json"
PERSONAS_DIR = _resolve_env_path("IZVOR_PERSONAS_DIR", CONFIG_DIR / "personas")
WORKFLOWS_DIR = _resolve_env_path("IZVOR_WORKFLOWS_DIR", CONFIG_DIR / "workflows")
SKILLS_DIR = _resolve_env_path("IZVOR_SKILLS_DIR", CONFIG_DIR / "skills")
AVATARS_DIR = _resolve_env_path("IZVOR_AVATARS_DIR", CONFIG_DIR / "avatars")
RAG_KNOWLEDGE_DIR = _resolve_env_path("IZVOR_RAG_KNOWLEDGE_DIR", CONFIG_DIR / "rag_knowledge")

# 2. engine/ Pillar (Immutable Core Engine & Built-in System Assets)
ENGINE_SETTINGS_DIR = _resolve_env_path("IZVOR_ENGINE_SETTINGS_DIR", ENGINE_DIR / "settings")
DEFAULT_SYSTEM_AGENT_SETTINGS_FILE = ENGINE_SETTINGS_DIR / "system_agent_settings.json"
SYSTEM_AGENT_SETTINGS_FILE = _resolve_env_path(
    "IZVOR_SYSTEM_AGENT_SETTINGS_FILE", DATA_DIR / "settings" / "system_agent_settings.json"
)
PROMPTS_DIR = _resolve_env_path("IZVOR_PROMPTS_DIR", ENGINE_DIR / "prompts")
SYSTEM_AGENTS_DIR = PROMPTS_DIR / "system_agents"
SHARED_PROMPTS_DIR = PROMPTS_DIR / "shared"
ENGINE_SKILLS_DIR = _resolve_env_path("IZVOR_ENGINE_SKILLS_DIR", ENGINE_DIR / "skills" / "builtin")
ENGINE_WORKFLOWS_DIR = _resolve_env_path("IZVOR_ENGINE_WORKFLOWS_DIR", ENGINE_DIR / "workflows")
ENGINE_AVATARS_DIR = ENGINE_DIR / "avatars"

# 3. data/ Pillar (Runtime & UI-Managed State)
STATE_DIR = _resolve_env_path("IZVOR_STATE_DIR", DATA_DIR / "state")
APP_SETTINGS_FILE = _resolve_env_path("IZVOR_APP_SETTINGS_FILE", STATE_DIR / "app_settings.json")
DB_PATH = _resolve_env_path("IZVOR_DB_PATH", DATA_DIR / "izvor.db")
BACKUPS_DIR = _resolve_env_path("IZVOR_BACKUPS_DIR", DATA_DIR / "backups")
SYSTEM_BACKUP_FILE = BACKUPS_DIR / "system_settings_backup.json"
AGENT_BACKUP_FILE = BACKUPS_DIR / "agent_settings_backup.json"
SYSTEM_AGENT_BACKUP_FILE = BACKUPS_DIR / "system_agent_settings_backup.json"
APP_BACKUP_FILE = BACKUPS_DIR / "app_settings_backup.json"
WORKFLOWS_BACKUP_DIR = _resolve_env_path("IZVOR_WORKFLOWS_BACKUP_DIR", BACKUPS_DIR / "workflows")

PRICING_DIR = _resolve_env_path("IZVOR_PRICING_DIR", DATA_DIR / "pricing")
MODEL_PROVIDER_CATALOG_FILE = PRICING_DIR / "model_provider_catalog.json"
PRICING_CACHE_FILE = PRICING_DIR / "model_pricing_cache.json"
BASELINE_PRICES_FILE = PRICING_DIR / "baseline_prices.json"

THREAD_DATA_DIR = _resolve_env_path("IZVOR_THREAD_DATA_DIR", DATA_DIR / "thread")
USAGE_STORE_FILE = THREAD_DATA_DIR / "thread_usage_data.json"

RAG_CACHE_DIR = _resolve_env_path("IZVOR_RAG_CACHE_DIR", DATA_DIR / ".rag_cache")
LANCEDB_DIR = _resolve_env_path("IZVOR_LANCEDB_DIR", RAG_CACHE_DIR / "lancedb")
MANIFEST_PATH = _resolve_env_path("IZVOR_RAG_MANIFEST_PATH", RAG_CACHE_DIR / "file_manifest.json")

AUDIT_DIR = _resolve_env_path("IZVOR_AUDIT_DIR", DATA_DIR / "audit")
DOWNLOADS_DIR = _resolve_env_path("IZVOR_DOWNLOADS_DIR", DATA_DIR / "downloads")
FLAT_HISTORIES_DIR = _resolve_env_path("IZVOR_HISTORIES_DIR", DATA_DIR / "flat-histories")

# Sandboxed Resources & System Artifacts
AGENTS_RESOURCES_DIR = _resolve_env_path("IZVOR_AGENTS_RESOURCES_DIR", PROJECT_ROOT / "agents_resources")
THREAD_NOTES_DIR = _resolve_env_path("IZVOR_THREAD_NOTES_DIR", AGENTS_RESOURCES_DIR / "thread_notes")
SYSTEM_RESOURCES_DIR = _resolve_env_path("IZVOR_SYSTEM_RESOURCES_DIR", PROJECT_ROOT / "system_resources")


def get_downloads_dir() -> Path:
    """Dynamically resolves the active downloads directory taking IZVOR_DOWNLOADS_DIR overrides into account."""
    return _resolve_env_path("IZVOR_DOWNLOADS_DIR", DATA_DIR / "downloads")


def get_pricing_cache_file() -> Path:
    """Dynamically resolves the pricing cache file taking IZVOR_PRICING_CACHE_FILE overrides into account."""
    return _resolve_env_path("IZVOR_PRICING_CACHE_FILE", PRICING_DIR / "model_pricing_cache.json")


__all__ = [
    "AGENT_BACKUP_FILE",
    "AGENT_SETTINGS_FILE",
    "AGENTS_RESOURCES_DIR",
    "APP_BACKUP_FILE",
    "APP_SETTINGS_FILE",
    "AUDIT_DIR",
    "AVATARS_DIR",
    "BACKUPS_DIR",
    "BASELINE_PRICES_FILE",
    "CONFIG_DIR",
    "DATA_DIR",
    "DB_PATH",
    "DEFAULT_SYSTEM_AGENT_SETTINGS_FILE",
    "DOWNLOADS_DIR",
    "ENGINE_AVATARS_DIR",
    "ENGINE_DIR",
    "ENGINE_SETTINGS_DIR",
    "ENGINE_SKILLS_DIR",
    "ENGINE_WORKFLOWS_DIR",
    "FLAT_HISTORIES_DIR",
    "LANCEDB_DIR",
    "MANIFEST_PATH",
    "MODEL_PROVIDER_CATALOG_FILE",
    "PERSONAS_DIR",
    "PRICING_CACHE_FILE",
    "PRICING_DIR",
    "PROJECT_ROOT",
    "PROMPTS_DIR",
    "RAG_CACHE_DIR",
    "RAG_KNOWLEDGE_DIR",
    "SETTINGS_DIR",
    "SHARED_PROMPTS_DIR",
    "SKILLS_DIR",
    "STATE_DIR",
    "SYSTEM_AGENT_BACKUP_FILE",
    "SYSTEM_AGENT_SETTINGS_FILE",
    "SYSTEM_AGENTS_DIR",
    "SYSTEM_BACKUP_FILE",
    "SYSTEM_RESOURCES_DIR",
    "SYSTEM_SETTINGS_FILE",
    "THREAD_DATA_DIR",
    "THREAD_NOTES_DIR",
    "USAGE_STORE_FILE",
    "WORKFLOWS_BACKUP_DIR",
    "WORKFLOWS_DIR",
    "_resolve_env_path",
    "get_downloads_dir",
    "get_pricing_cache_file",
]
