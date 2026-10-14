import re


def wrap_untrusted_content(text: str) -> str:
    """Wraps untrusted external data to prevent indirect prompt injection."""
    if not text:
        return ""
    # Strip any existing tags to prevent injection of closing tags or boundary breakouts
    clean_text = re.sub(r"<\s*/?\s*untrusted_file_content[^>]*>", "", str(text), flags=re.IGNORECASE)
    return f"<untrusted_file_content>\n{clean_text}\n</untrusted_file_content>"
