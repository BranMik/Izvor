import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import (
    CHUNK_OVERLAP_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    get_rag_setting,
)
from .text_splitter import RecursiveTextSplitter

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    chunk_id: str
    agent_id: str
    doc_id: str
    filename: str
    section_header: str
    text: str


def sanitize_id(val: str) -> str:
    """Replaces non-alphanumeric characters and appends a collision-free SHA-256 suffix."""
    raw = str(val or "")
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_") or "doc"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{digest}"


class DocumentParser:
    """
    Parses Markdown, PDF, and plain text files into semantic chunks
    with hierarchical section header metadata and chunk IDs.
    """

    def __init__(
        self,
        max_chunk_chars: int | None = None,
        chunk_overlap_chars: int | None = None,
        min_chunk_chars: int | None = None,
    ) -> None:
        self.max_chunk_chars = (
            max_chunk_chars
            if max_chunk_chars is not None
            else int(get_rag_setting("max_chunk_chars", MAX_CHUNK_CHARS))
        )
        self.chunk_overlap_chars = (
            chunk_overlap_chars
            if chunk_overlap_chars is not None
            else int(get_rag_setting("chunk_overlap_chars", CHUNK_OVERLAP_CHARS))
        )
        self.min_chunk_chars = (
            min_chunk_chars
            if min_chunk_chars is not None
            else int(get_rag_setting("min_chunk_chars", MIN_CHUNK_CHARS))
        )
        self._text_splitter: RecursiveTextSplitter | None = None

    @property
    def text_splitter(self) -> RecursiveTextSplitter:
        if (
            self._text_splitter is None
            or self._text_splitter.chunk_size != self.max_chunk_chars
            or self._text_splitter.chunk_overlap != self.chunk_overlap_chars
            or self._text_splitter.min_chunk_size != self.min_chunk_chars
        ):
            self._text_splitter = RecursiveTextSplitter(
                chunk_size=self.max_chunk_chars,
                chunk_overlap=self.chunk_overlap_chars,
                min_chunk_size=self.min_chunk_chars,
            )
        return self._text_splitter

    def parse_file(
        self, file_path: Path, doc_id: str, agent_id: str
    ) -> list[DocumentChunk]:
        suffix = file_path.suffix.lower()
        filename = file_path.name
        if suffix in (".md", ".markdown"):
            return self._parse_markdown(file_path, doc_id, agent_id, filename)
        elif suffix == ".pdf":
            return self._parse_pdf(file_path, doc_id, agent_id, filename)
        elif suffix == ".txt":
            return self._parse_text(file_path, doc_id, agent_id, filename)
        else:
            logger.warning(f"[RAG Parser] Unsupported file format: {file_path}")
            return []

    def _parse_markdown(
        self, file_path: Path, doc_id: str, agent_id: str, filename: str
    ) -> list[DocumentChunk]:
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="latin-1", errors="replace")

        # Split content by markdown headings outside fenced code blocks (``` / ~~~)
        fence_re = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
        heading_re = re.compile(r"^(#{1,6}\s+.+?)\s*$")
        sections: list[tuple[str, str]] = []
        current_header = "Overview"
        buf: list[str] = []
        fence_char: str | None = None
        fence_len = 0

        for line in content.splitlines(keepends=True):
            stripped_nl = line.rstrip("\r\n")
            f_match = fence_re.match(stripped_nl)
            if f_match:
                marker = f_match.group(1)
                rest = f_match.group(2)
                m_char = marker[0]
                if fence_char is None:
                    if m_char != "`" or "`" not in rest:
                        fence_char = m_char
                        fence_len = len(marker)
                elif m_char == fence_char and len(marker) >= fence_len and not rest.strip():
                    fence_char = None
                    fence_len = 0
                buf.append(line)
                continue

            if fence_char is None:
                h_match = heading_re.match(stripped_nl)
                if h_match:
                    body = "".join(buf).strip()
                    if body:
                        sections.append((current_header, body))
                    current_header = h_match.group(1).strip()
                    buf = []
                    continue

            buf.append(line)

        trailing_body = "".join(buf).strip()
        if trailing_body:
            sections.append((current_header, trailing_body))

        if not sections and content.strip():
            sections.append(("Overview", content.strip()))

        chunks: list[DocumentChunk] = []
        base_id = sanitize_id(doc_id)
        chunk_idx = 1
        doc_token_mapping: dict[str, str] = {}

        for header, body in sections:
            sub_chunks = self._chunk_text(body, token_mapping=doc_token_mapping)
            for sub_text in sub_chunks:
                cid = f"{base_id}_c{chunk_idx:03d}"
                chunks.append(
                    DocumentChunk(
                        chunk_id=cid,
                        agent_id=agent_id,
                        doc_id=doc_id,
                        filename=filename,
                        section_header=header,
                        text=sub_text,
                    )
                )
                chunk_idx += 1

        return chunks

    def _parse_pdf(
        self, file_path: Path, doc_id: str, agent_id: str, filename: str
    ) -> list[DocumentChunk]:
        try:
            import pypdf
        except ImportError:
            logger.error("[RAG Parser] pypdf is not installed. Skipping PDF parsing.")
            return []

        chunks: list[DocumentChunk] = []
        base_id = sanitize_id(doc_id)
        chunk_idx = 1
        doc_token_mapping: dict[str, str] = {}

        try:
            reader = pypdf.PdfReader(str(file_path))
            for page_num, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                page_text = unicodedata.normalize("NFKD", page_text).strip()
                if not page_text:
                    continue

                header = f"Page {page_num}"
                sub_chunks = self._chunk_text(page_text, token_mapping=doc_token_mapping)
                for sub_text in sub_chunks:
                    cid = f"{base_id}_c{chunk_idx:03d}"
                    chunks.append(
                        DocumentChunk(
                            chunk_id=cid,
                            agent_id=agent_id,
                            doc_id=doc_id,
                            filename=filename,
                            section_header=header,
                            text=sub_text,
                        )
                    )
                    chunk_idx += 1
        except Exception as e:
            logger.error(f"[RAG Parser] Failed to parse PDF {file_path}: {e}")
            raise

        return chunks

    def _parse_text(
        self, file_path: Path, doc_id: str, agent_id: str, filename: str
    ) -> list[DocumentChunk]:
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="latin-1", errors="replace")

        content = content.strip()
        if not content:
            return []

        chunks: list[DocumentChunk] = []
        base_id = sanitize_id(doc_id)
        doc_token_mapping: dict[str, str] = {}
        sub_chunks = self._chunk_text(content, token_mapping=doc_token_mapping)
        for chunk_idx, sub_text in enumerate(sub_chunks, start=1):
            cid = f"{base_id}_c{chunk_idx:03d}"
            chunks.append(
                DocumentChunk(
                    chunk_id=cid,
                    agent_id=agent_id,
                    doc_id=doc_id,
                    filename=filename,
                    section_header="General",
                    text=sub_text,
                )
            )

        return chunks

    def _chunk_text(self, text: str, token_mapping: dict[str, str] | None = None) -> list[str]:
        """
        Splits a text body into semantic chunks using RecursiveTextSplitter.
        Reuses document-level token_mapping across sections/chunks so repeated PII aliases stay consistent.
        """
        from services.security.pii_masker import pii_masker

        if pii_masker.should_mask_at_ingestion():
            text, updated_mapping = pii_masker.mask(text, existing_mapping=token_mapping)
            if token_mapping is not None and updated_mapping:
                token_mapping.update(updated_mapping)
        return self.text_splitter.split_text(text)

