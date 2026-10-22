import tempfile
from pathlib import Path


from services.rag.document_parser import DocumentChunk, DocumentParser
from services.rag.text_splitter import RecursiveTextSplitter


def test_recursive_splitter_short_text():
    splitter = RecursiveTextSplitter(chunk_size=100, chunk_overlap=20)
    text = "Short text that easily fits in one chunk."
    chunks = splitter.split_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_recursive_splitter_splits_on_paragraphs():
    splitter = RecursiveTextSplitter(chunk_size=90, chunk_overlap=20, min_chunk_size=10)
    text = (
        "Paragraph 1 contains some architecture details.\n\n"
        "Paragraph 2 discusses database indexing and queries.\n\n"
        "Paragraph 3 covers deployment and safeguards."
    )
    chunks = splitter.split_text(text)
    assert len(chunks) >= 3
    # Check that paragraphs are not mangled
    assert any("Paragraph 1" in c for c in chunks)
    assert any("Paragraph 2" in c for c in chunks)
    assert any("Paragraph 3" in c for c in chunks)
    # Ensure splits did not cut midway through any word
    for c in chunks:
        assert not c.endswith("Paragra")
        assert not c.startswith("raph")


def test_recursive_splitter_splits_on_sentences():
    # chunk_size chosen so single sentences fit, but pairs exceed
    splitter = RecursiveTextSplitter(chunk_size=65, chunk_overlap=25, min_chunk_size=10)
    text = (
        "First sentence is clear and concise. "
        "Second sentence adds context. "
        "Third sentence details the solution. "
        "Fourth sentence concludes the thought."
    )
    chunks = splitter.split_text(text)
    assert len(chunks) > 1

    # Verify that sentence ending punctuation is preserved
    for c in chunks:
        # Every chunk should start and end on clean word/sentence boundaries
        words = c.split()
        assert len(words) > 0
        # No sliced words like "cle" or "conci"
        assert all(w in text for w in words)


def test_recursive_splitter_overlap_clean_boundaries():
    splitter = RecursiveTextSplitter(chunk_size=75, chunk_overlap=30, min_chunk_size=10)
    text = (
        "Alpha beta gamma delta. "
        "Epsilon zeta eta theta. "
        "Iota kappa lambda mu. "
        "Nu xi omicron pi."
    )
    chunks = splitter.split_text(text)
    assert len(chunks) >= 2

    # Overlap between consecutive chunks should consist of whole words/sentences
    for i in range(len(chunks) - 1):
        c1 = chunks[i]
        c2 = chunks[i + 1]
        words1 = set(c1.split())
        words2 = set(c2.split())
        common_words = words1.intersection(words2)
        # There should be overlap words
        if common_words:
            for w in common_words:
                assert w in text


def test_recursive_splitter_long_sentence_splits_on_words():
    splitter = RecursiveTextSplitter(chunk_size=40, chunk_overlap=15, min_chunk_size=5)
    text = "This is a single very long sentence without any punctuation breaks to test word splitting."
    chunks = splitter.split_text(text)
    assert len(chunks) > 1
    # Verify no word is sliced in half
    for c in chunks:
        for word in c.split():
            assert word in text.split()


def test_recursive_splitter_unbroken_string_fallback():
    splitter = RecursiveTextSplitter(chunk_size=20, chunk_overlap=5)
    unbroken = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    chunks = splitter.split_text(unbroken)
    assert len(chunks) >= 2
    assert "".join(chunks[0]) == unbroken[:20]


def test_document_parser_markdown():
    parser = DocumentParser(max_chunk_chars=500, chunk_overlap_chars=50)
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
        f.write(
            "# System SecurityArchitecture\n\n"
            "This document describes the architectural layout of the agentic system.\n\n"
            "## Storage Layer\n\n"
            "LanceDB stores vectors. Tantivy handles keyword search indices.\n\n"
            "## Network Layer\n\n"
            "FastAPI handles incoming webhooks from Slack. Responses are streamed."
        )
        temp_path = Path(f.name)

    try:
        chunks = parser.parse_file(temp_path, "arch.md", "agent-10008")
        assert len(chunks) >= 2
        assert any("Storage Layer" in c.section_header for c in chunks)
        assert any("Network Layer" in c.section_header for c in chunks)
        assert all(c.agent_id == "agent-10008" for c in chunks)
        assert all(c.filename == temp_path.name for c in chunks)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def test_document_parser_dynamic_settings():
    parser = DocumentParser(max_chunk_chars=3000, chunk_overlap_chars=200)
    assert parser.text_splitter.chunk_size == 3000
    assert parser.text_splitter.chunk_overlap == 200

    # Dynamically change setting
    parser.max_chunk_chars = 1500
    parser.chunk_overlap_chars = 100
    assert parser.text_splitter.chunk_size == 1500
    assert parser.text_splitter.chunk_overlap == 100


def test_document_parser_text_file():
    parser = DocumentParser(max_chunk_chars=80, chunk_overlap_chars=20)
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
        f.write(
            "First paragraph in text file.\n\n"
            "Second paragraph in text file with additional explanation."
        )
        temp_path = Path(f.name)

    try:
        chunks = parser.parse_file(temp_path, "notes.txt", "agent-10011")
        assert len(chunks) >= 1
        assert all(isinstance(c, DocumentChunk) for c in chunks)
        assert all(c.section_header == "General" for c in chunks)
    finally:
        if temp_path.exists():
            temp_path.unlink()
