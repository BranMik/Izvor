import os
from pathlib import Path

# Base project paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
RAG_KNOWLEDGE_DIR = (BASE_DIR / "rag_knowledge").resolve()
CACHE_DIR = (BASE_DIR / "data" / ".rag_cache").resolve()
LANCEDB_DIR = (CACHE_DIR / "lancedb").resolve()
MANIFEST_PATH = (CACHE_DIR / "file_manifest.json").resolve()

# Table and search configuration
TABLE_NAME = "agent_knowledge"
DEFAULT_TOP_K = 4

# Embedding model settings (LiteLLM with Gemini gemini-embedding-001 by default)
EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "gemini/gemini-embedding-001")
EMBEDDING_DIM = int(os.getenv("RAG_EMBEDDING_DIM", "3072"))
EMBEDDING_BATCH_SIZE = int(os.getenv("RAG_EMBEDDING_BATCH_SIZE", "32"))

# Semantic chunking configuration
MAX_CHUNK_CHARS = int(os.getenv("RAG_MAX_CHUNK_CHARS", "1200"))
CHUNK_OVERLAP_CHARS = int(os.getenv("RAG_CHUNK_OVERLAP_CHARS", "150"))
MIN_CHUNK_CHARS = int(os.getenv("RAG_MIN_CHUNK_CHARS", "80"))

# Supported document extensions
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}
