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
    """Replaces non-alphanumeric characters with underscores."""
    return re.sub(r"[^a-zA-Z0-9_]+", "_", val).strip("_")


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

        # Split content by markdown headings (# H1, ## H2, ### H3, etc.)
        heading_pattern = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)
        parts = heading_pattern.split(content)

        sections: list[tuple[str, str]] = []
        current_header = "Overview"

        if parts:
            lead_text = parts[0].strip()
            if lead_text:
                sections.append((current_header, lead_text))

            i = 1
            while i < len(parts):
                current_header = parts[i].strip()
                body = parts[i + 1].strip() if (i + 1) < len(parts) else ""
                if body:
                    sections.append((current_header, body))
                i += 2

        if not sections and content.strip():
            sections.append(("Overview", content.strip()))

        chunks: list[DocumentChunk] = []
        base_id = sanitize_id(doc_id)
        chunk_idx = 1

        for header, body in sections:
            sub_chunks = self._chunk_text(body)
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

        try:
            reader = pypdf.PdfReader(str(file_path))
            for page_num, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                page_text = unicodedata.normalize("NFKD", page_text).strip()
                if not page_text:
                    continue

                header = f"Page {page_num}"
                sub_chunks = self._chunk_text(page_text)
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"[RAG Parser] Failed to parse PDF {file_path}: {e}")

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
        sub_chunks = self._chunk_text(content)
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

    def _chunk_text(self, text: str) -> list[str]:
        """
        Splits a text body into semantic chunks using RecursiveTextSplitter.
        Ensures splits cleanly happen at paragraph, line, or sentence breaks
        rather than midway through sentences or words.
        """
        from services.security.pii_masker import pii_masker
        if pii_masker.should_mask_at_ingestion():
            text, _ = pii_masker.mask(text)
        return self.text_splitter.split_text(text)

