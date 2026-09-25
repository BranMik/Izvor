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
            sub_chunks = self._chunk_text(body, header)
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
                sub_chunks = self._chunk_text(page_text, header)
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
        sub_chunks = self._chunk_text(content, "General")
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

    def _chunk_text(self, text: str, header_context: str) -> list[str]:
        """
        Splits a text body into chunks of max_chunk_chars with chunk_overlap_chars.
        Prepends header context to provide semantic awareness if not already present.
        """
        if len(text) <= self.max_chunk_chars:
            return [text]

        # Split along paragraphs first, then sentences if necessary
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_len = 0

        for p in paragraphs:
            if current_len + len(p) + 2 > self.max_chunk_chars and current_chunk:
                joined = "\n\n".join(current_chunk)
                chunks.append(joined)
                # Keep overlap from the end of the previous chunk
                overlap_text = joined[-self.chunk_overlap_chars:] if len(joined) > self.chunk_overlap_chars else ""
                current_chunk = [overlap_text, p] if overlap_text else [p]
                current_len = sum(len(x) for x in current_chunk) + len(current_chunk) * 2
            else:
                current_chunk.append(p)
                current_len += len(p) + 2

        if current_chunk:
            joined = "\n\n".join(c for c in current_chunk if c.strip())
            if joined.strip():
                chunks.append(joined)

        # Fallback if any single chunk is still exceeding max_chunk_chars
        final_chunks: list[str] = []
        for ch in chunks:
            if len(ch) <= self.max_chunk_chars:
                final_chunks.append(ch)
            else:
                start = 0
                while start < len(ch):
                    end = start + self.max_chunk_chars
                    final_chunks.append(ch[start:end])
                    start = end - self.chunk_overlap_chars
                    if start >= len(ch) - self.min_chunk_chars:
                        break

        return final_chunks
