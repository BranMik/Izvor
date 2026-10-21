"""
core/paths.py — Canonical Filesystem Path Definitions for Izvor.

Provides centralized path constants with zero internal application imports
(only standard library `os` and `pathlib.Path`) to prevent circular dependency
deadlocks across core, orchestration, services, and agent layers.
"""
from __future__ import annotations

import os
from pathlib import Path

# Project Root anchor (directory containing main.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


# Core Data & Settings Directories
DATA_DIR = _resolve_env_path("IZVOR_DATA_DIR", PROJECT_ROOT / "data")
SETTINGS_DIR = _resolve_env_path("IZVOR_SETTINGS_DIR", DATA_DIR / "settings")
BACKUPS_DIR = _resolve_env_path("IZVOR_BACKUPS_DIR", DATA_DIR / "backups")
WORKFLOWS_DIR = _resolve_env_path("IZVOR_WORKFLOWS_DIR", DATA_DIR / "workflows")
WORKFLOWS_BACKUP_DIR = _resolve_env_path("IZVOR_WORKFLOWS_BACKUP_DIR", BACKUPS_DIR / "workflows")
PRICING_DIR = _resolve_env_path("IZVOR_PRICING_DIR", DATA_DIR / "pricing")
THREAD_DATA_DIR = _resolve_env_path("IZVOR_THREAD_DATA_DIR", DATA_DIR / "thread")
AUDIT_DIR = _resolve_env_path("IZVOR_AUDIT_DIR", PROJECT_ROOT / "audit")
DOWNLOADS_DIR = _resolve_env_path("IZVOR_DOWNLOADS_DIR", PROJECT_ROOT / "downloads")

# Database & SQLite State
DB_PATH = _resolve_env_path("IZVOR_DB_PATH", DATA_DIR / "izvor.db")

# Canonical Configuration Files
SYSTEM_SETTINGS_FILE = SETTINGS_DIR / "system_settings.json"
AGENT_SETTINGS_FILE = SETTINGS_DIR / "agent_settings.json"
APP_SETTINGS_FILE = SETTINGS_DIR / "app_settings.json"

# Backups
SYSTEM_BACKUP_FILE = BACKUPS_DIR / "system_settings_backup.json"
AGENT_BACKUP_FILE = BACKUPS_DIR / "agent_settings_backup.json"
APP_BACKUP_FILE = BACKUPS_DIR / "app_settings_backup.json"

# Pricing & Catalog Files
MODEL_PROVIDER_CATALOG_FILE = PRICING_DIR / "model_provider_catalog.json"
PRICING_CACHE_FILE = PRICING_DIR / "model_pricing_cache.json"
BASELINE_PRICES_FILE = PRICING_DIR / "baseline_prices.json"

# Prompts & Personas
PROMPTS_DIR = _resolve_env_path("IZVOR_PROMPTS_DIR", PROJECT_ROOT / "prompts")
PERSONAS_DIR = _resolve_env_path("IZVOR_PERSONAS_DIR", PROMPTS_DIR / "personas")
SYSTEM_AGENTS_DIR = PROMPTS_DIR / "system_agents"

# Resources & Thread Artifacts
AGENTS_RESOURCES_DIR = _resolve_env_path("IZVOR_AGENTS_RESOURCES_DIR", PROJECT_ROOT / "agents_resources")
THREAD_NOTES_DIR = _resolve_env_path("IZVOR_THREAD_NOTES_DIR", AGENTS_RESOURCES_DIR / "thread_notes")
SYSTEM_RESOURCES_DIR = _resolve_env_path("IZVOR_SYSTEM_RESOURCES_DIR", PROJECT_ROOT / "system_resources")

# Runtime & Flat Histories
FLAT_HISTORIES_DIR = _resolve_env_path("IZVOR_HISTORIES_DIR", PROJECT_ROOT / "flat-histories")
THREAD_SKILLS_FILE = THREAD_DATA_DIR / "thread_skills.json"
USAGE_STORE_FILE = THREAD_DATA_DIR / "thread_usage_data.json"

# RAG Knowledge & Vector Cache
RAG_KNOWLEDGE_DIR = _resolve_env_path("IZVOR_RAG_KNOWLEDGE_DIR", PROJECT_ROOT / "rag_knowledge")
RAG_CACHE_DIR = _resolve_env_path("IZVOR_RAG_CACHE_DIR", DATA_DIR / ".rag_cache")
LANCEDB_DIR = _resolve_env_path("IZVOR_LANCEDB_DIR", RAG_CACHE_DIR / "lancedb")
MANIFEST_PATH = _resolve_env_path("IZVOR_RAG_MANIFEST_PATH", RAG_CACHE_DIR / "file_manifest.json")

__all__ = [
    "AGENT_BACKUP_FILE",
    "AGENT_SETTINGS_FILE",
    "AGENTS_RESOURCES_DIR",
    "APP_BACKUP_FILE",
    "APP_SETTINGS_FILE",
    "AUDIT_DIR",
    "BACKUPS_DIR",
    "BASELINE_PRICES_FILE",
    "DATA_DIR",
    "DB_PATH",
    "DOWNLOADS_DIR",
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
    "SYSTEM_AGENTS_DIR",
    "SYSTEM_BACKUP_FILE",
    "SYSTEM_RESOURCES_DIR",
    "SYSTEM_SETTINGS_FILE",
    "THREAD_DATA_DIR",
    "THREAD_NOTES_DIR",
    "THREAD_SKILLS_FILE",
    "USAGE_STORE_FILE",
    "WORKFLOWS_BACKUP_DIR",
    "WORKFLOWS_DIR",
    "_resolve_env_path",
]
