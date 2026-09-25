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
SYSTEM_SETTINGS_PATH = (BASE_DIR / "data" / "system_settings.json").resolve()

logger = logging.getLogger(__name__)

# Key aliases for user-friendly configuration
_RAG_KEY_ALIASES: dict[str, str] = {
    "set_default_top_k": "default_top_k",
    "top_k": "default_top_k",
}


def load_rag_settings_from_file() -> dict[str, Any]:
    """Reads the 'rag' section from data/system_settings.json."""
    if not SYSTEM_SETTINGS_PATH.exists():
        return {}
    try:
        with open(SYSTEM_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            rag = data.get("rag")
            return rag if isinstance(rag, dict) else {}
    except Exception as e:
        logger.debug(f"[RAG Config] Could not read system_settings.json: {e}")
        return {}


def get_rag_setting(key: str, default: Any = None) -> Any:
    """
    Reads RAG setting with precedence:
    1. Environment variable (e.g. RAG_MAX_CHUNK_CHARS)
    2. 'rag' section in data/system_settings.json
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

    # Read from data/system_settings.json
    rag_data = load_rag_settings_from_file()
    key_lower = key.lower()
    resolved_key = _RAG_KEY_ALIASES.get(key_lower, key_lower)
    for k, v in rag_data.items():
        if k.lower() == resolved_key or k.lower() == key_lower:
            if isinstance(default, int) and v is not None:
                try:
                    return int(v)
                except (ValueError, TypeError):
                    pass
            return v

    return default


# Table and search configuration
TABLE_NAME = "agent_knowledge"
DEFAULT_TOP_K = int(get_rag_setting("default_top_k", 3))

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
