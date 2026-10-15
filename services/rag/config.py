import json
import logging
import os
from pathlib import Path
from typing import Any

# Base project paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
RAG_KNOWLEDGE_DIR = (BASE_DIR / "rag_knowledge").resolve()
CACHE_DIR = (BASE_DIR / "data" / ".rag_cache").resolve()
LANCEDB_DIR = (CACHE_DIR / "lancedb").resolve()
MANIFEST_PATH = (CACHE_DIR / "file_manifest.json").resolve()
SYSTEM_SETTINGS_PATH = (BASE_DIR / "data" / "settings" / "system_settings.json").resolve()

from orchestration.system_settings import system_settings_manager

logger = logging.getLogger(__name__)

# Key aliases for user-friendly configuration
_RAG_KEY_ALIASES: dict[str, str] = {
    "set_default_top_k": "default_top_k",
    "top_k": "default_top_k",
}


def load_rag_settings_from_file() -> dict[str, Any]:
    """Reads the 'rag' section via SystemSettingsManager SSOT."""
    try:
        return system_settings_manager.get_rag_config()
    except Exception as e:
        logger.debug(f"[RAG Config] Could not read rag settings from system_settings_manager: {e}")
        return {}


def get_rag_setting(key: str, default: Any = None) -> Any:
    """
    Reads RAG setting with precedence:
    1. Environment variable (e.g. RAG_MAX_CHUNK_CHARS)
    2. 'rag' section via SystemSettingsManager SSOT
    3. Fallback default
    """
    env_key = f"RAG_{key.upper()}"
    env_val = os.getenv(env_key)
    if env_val is not None:
        if isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                pass
        return env_val

    try:
        val = system_settings_manager.get_rag_setting(key, default)
        if isinstance(default, int) and val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return val
    except Exception as e:
        logger.debug(f"[RAG Config] Error getting rag setting '{key}' from manager: {e}")
        return default


# Table and search configuration
TABLE_NAME = "agent_knowledge"
DEFAULT_TOP_K = int(get_rag_setting("default_top_k", 3))

# PII masking configuration ("AT_RETRIEVAL", "AT_INGESTION", "NONE")
VALID_PII_MASKING_MODES = {"AT_RETRIEVAL", "AT_INGESTION", "NONE"}
PII_MASKING = str(get_rag_setting("pii_masking", "AT_RETRIEVAL")).strip().upper()


def get_pii_masking_mode() -> str:
    """Returns current PII masking mode ('AT_RETRIEVAL', 'AT_INGESTION', 'NONE')."""
    mode = str(get_rag_setting("pii_masking", "AT_RETRIEVAL")).strip().upper()
    if mode in VALID_PII_MASKING_MODES:
        return mode
    return "AT_RETRIEVAL"


# Embedding model settings (LiteLLM with Gemini gemini-embedding-001 by default)
EMBEDDING_MODEL = str(get_rag_setting("embedding_model", "gemini/gemini-embedding-001"))
EMBEDDING_DIM = int(get_rag_setting("embedding_dim", 3072))
EMBEDDING_BATCH_SIZE = int(get_rag_setting("embedding_batch_size", 32))

# Semantic chunking configuration
MAX_CHUNK_CHARS = int(get_rag_setting("max_chunk_chars", 1500))
CHUNK_OVERLAP_CHARS = int(get_rag_setting("chunk_overlap_chars", 300))
MIN_CHUNK_CHARS = int(get_rag_setting("min_chunk_chars", 200))

# Supported document extensions
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}

# Prompt template and citation instructions
CITATION_INSTRUCTIONS_PATH = (
    Path(__file__).resolve().parent / "citation_instructions.md"
).resolve()

# Guardrail Threshold
GUARDRAIL_THRESHOLD = float(get_rag_setting("guardrail_threshold", 0.85))
