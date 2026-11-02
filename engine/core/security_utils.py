import re


def wrap_untrusted_content(text: str) -> str:
    """Wraps untrusted external data to prevent indirect prompt injection."""
    if not text:
        return ""
    # Strip any existing tags to prevent injection of closing tags or boundary breakouts
    clean_text = str(text)
    pattern = re.compile(
        r"<\s*/?\s*untrusted_file_content(?:\s+[^>\"']*(?:\"[^\"]*\"|'[^']*')*)*\s*/?>|"
        r"<\s*/?\s*untrusted_file_content[^>]*>",
        re.IGNORECASE,
    )
    prev = None
    while prev != clean_text:
        prev = clean_text
        clean_text = pattern.sub("", clean_text)
    return f"<untrusted_file_content>\n{clean_text}\n</untrusted_file_content>"
