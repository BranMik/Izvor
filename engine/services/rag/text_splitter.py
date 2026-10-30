import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)


class RecursiveTextSplitter:
    """
    Recursively splits text into chunks along semantic boundaries:
    paragraphs ("\\n\\n"), lines ("\\n"), sentences (punctuation lookbehind),
    words (whitespace), and characters as a last resort.

    Ensures splits happen cleanly at paragraph or sentence breaks rather than
    midway through a sentence or word, with clean overlap handling.
    """

    def __init__(
        self,
        chunk_size: int = 3500,
        chunk_overlap: int = 300,
        min_chunk_size: int = 80,
        separators: list[str] | None = None,
        length_function: Callable[[str], int] = len,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) must be strictly less than chunk_size ({chunk_size})"
            )

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        self.length_function = length_function
        self.separators = separators or [
            "\n\n",
            "\n",
            r"(?<=[.?!])\s+",
            r"\s+",
            "",
        ]

    def split_text(self, text: str) -> list[str]:
        """Splits the input text into a list of clean chunks."""
        text = text.strip()
        if not text:
            return []
        if self.length_function(text) <= self.chunk_size:
            return [text]

        pieces = self._recursive_split(text, self.separators)
        chunks = self._merge_pieces(pieces)
        return chunks

    def _split_with_sep(self, text: str, sep: str) -> list[str]:
        if sep == "":
            return list(text)

        # Enclose in capturing parentheses to keep delimiters attached to pieces
        if sep.startswith(r"(?<=") or sep.startswith("\\"):
            pattern = f"({sep})"
        else:
            pattern = f"({re.escape(sep)})"

        raw_parts = re.split(pattern, text)
        pieces: list[str] = []
        i = 0
        while i < len(raw_parts):
            part = raw_parts[i]
            delim = raw_parts[i + 1] if i + 1 < len(raw_parts) else ""
            combined = part + delim
            if combined:
                pieces.append(combined)
            i += 2
        return pieces

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        if not text:
            return []
        if self.length_function(text) <= self.chunk_size:
            return [text]

        chosen_sep = None
        remaining_seps: list[str] = []

        for i, sep in enumerate(separators):
            if sep == "":
                chosen_sep = ""
                remaining_seps = []
                break
            if sep.startswith(r"(?<=") or sep.startswith("\\"):
                if re.search(sep, text):
                    chosen_sep = sep
                    remaining_seps = separators[i + 1 :]
                    break
            elif sep in text:
                chosen_sep = sep
                remaining_seps = separators[i + 1 :]
                break

        if chosen_sep is None:
            chosen_sep = ""
            remaining_seps = []

        pieces = self._split_with_sep(text, chosen_sep)

        result: list[str] = []
        for p in pieces:
            if self.length_function(p) <= self.chunk_size:
                result.append(p)
            else:
                if remaining_seps:
                    result.extend(self._recursive_split(p, remaining_seps))
                else:
                    # Character slice fallback if no further separators exist
                    start = 0
                    while start < len(p):
                        end = min(start + self.chunk_size, len(p))
                        result.append(p[start:end])
                        if end == len(p):
                            break
                        start = end
        return result

    def _merge_pieces(self, pieces: list[str]) -> list[str]:
        chunks: list[str] = []
        current_docs: list[str] = []
        current_len = 0

        for piece in pieces:
            piece_len = self.length_function(piece)
            if current_len + piece_len > self.chunk_size and current_docs:
                chunk_str = "".join(current_docs).strip()
                if chunk_str:
                    chunks.append(chunk_str)

                # Keep overlap pieces from the end of current_docs
                while current_docs and (
                    current_len > self.chunk_overlap
                    or current_len + piece_len > self.chunk_size
                ):
                    popped = current_docs.pop(0)
                    current_len -= self.length_function(popped)

            current_docs.append(piece)
            current_len += piece_len

        if current_docs:
            chunk_str = "".join(current_docs).strip()
            if chunk_str:
                chunks.append(chunk_str)

        # Merge tiny trailing chunks under min_chunk_size into preceding chunk if feasible
        if len(chunks) > 1 and self.min_chunk_size > 0:
            filtered: list[str] = []
            for c in chunks:
                if len(c) >= self.min_chunk_size:
                    filtered.append(c)
                elif filtered:
                    if len(filtered[-1]) + len(c) + 1 <= self.chunk_size:
                        filtered[-1] = filtered[-1] + "\n\n" + c
                    else:
                        filtered.append(c)
                else:
                    filtered.append(c)
            chunks = filtered

        return chunks
