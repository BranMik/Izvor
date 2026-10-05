def wrap_untrusted_content(text: str) -> str:
    """Wraps untrusted external data to prevent indirect prompt injection."""
    if not text:
        return ""
    # Strip any existing tags to prevent injection of closing tags
    clean_text = str(text).replace("<untrusted_file_content>", "").replace("</untrusted_file_content>", "")
    return f"<untrusted_file_content>\n{clean_text}\n</untrusted_file_content>"
