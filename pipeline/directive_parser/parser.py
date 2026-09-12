import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.models import ExtractedIntent
from nlp.nl_patterns import (
    extract_excluded_agents,
    extract_file_instruction,
    extract_history_limit,
    extract_max_words_limit,
    extract_min_words_limit,
    is_stop_command,
)

logger = logging.getLogger(__name__)


@dataclass
class ParsedDirectives:
    """Consolidated container for all parsed directives from a user prompt."""

    cleaned_prompt: str
    max_words: int | None = None
    min_words: int | None = None
    history_limit: int | None = None
    excluded_agents: list[str] = field(default_factory=list)
    is_file_req: bool = False
    target_filename: str | None = None
    is_stop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleaned_prompt": self.cleaned_prompt,
            "max_words": self.max_words,
            "min_words": self.min_words,
            "history_limit": self.history_limit,
            "excluded_agents": self.excluded_agents,
            "is_file_req": self.is_file_req,
            "target_filename": self.target_filename,
            "is_stop": self.is_stop,
        }


class DirectiveParser:
    """
    Two-tier parser: fast regex/heuristics + micro-LLM fallback.
    """

    def __init__(self, extractor_client: Any = None):
        self._extractor_client = extractor_client

    async def parse(
        self,
        raw_text: str,
        available_files: list[dict[str, Any]] | None = None,
        is_mention_or_dm: bool = False,
    ) -> ParsedDirectives:
        """
        Parses raw text and returns a clean prompt and extracted directives.
        """
        # 1. Stop command check
        if is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm):
            return ParsedDirectives(cleaned_prompt=raw_text, is_stop=True)

        # 2. Fast-path deterministic extractions
        history_limit, text_cleaned = extract_history_limit(raw_text)
        max_words, text_cleaned = extract_max_words_limit(text_cleaned)
        min_words, text_cleaned = extract_min_words_limit(text_cleaned)
        excluded, text_cleaned = extract_excluded_agents(text_cleaned)

        is_file_req, target_filename, text_cleaned = extract_file_instruction(
            text_cleaned, available_files=available_files
        )

        # 3. Check if micro-LLM intent extraction should run
        from nlp.intent_extractor import (
            extract_user_intent,
            should_skip_intent_extractor,
        )

        has_slash_file = bool(
            re.search(r"/(?:read[-_]?file|file|attachment)", raw_text, re.IGNORECASE)
        )
        has_slash_max_words = bool(
            re.search(r"/(?:max_words|max-words|maxwords)", raw_text, re.IGNORECASE)
        )
        has_slash_history = bool(
            re.search(r"/history", raw_text, re.IGNORECASE)
        )
        has_thread_files = bool(available_files)

        if not should_skip_intent_extractor(
            raw_text=raw_text,
            is_file_cmd=has_slash_file,
            has_slash_max_words=has_slash_max_words,
            has_slash_history=has_slash_history,
            has_thread_files=has_thread_files,
        ):
            intent: ExtractedIntent = await extract_user_intent(
                text_cleaned, available_files=available_files
            )
            intent_dict = intent.to_dict() if hasattr(intent, "to_dict") else intent
            if max_words is None and intent_dict.get("max_words") is not None:
                max_words = intent_dict["max_words"]
            if min_words is None and intent_dict.get("min_words") is not None:
                min_words = intent_dict["min_words"]
            if history_limit is None and intent_dict.get("history") is not None:
                history_limit = intent_dict["history"]
            if not is_file_req and intent_dict.get("file_use"):
                is_file_req = True
                if not target_filename and intent_dict.get("target_file"):
                    target_filename = intent_dict["target_file"]
            for exc in intent_dict.get("excluded_agents", []):
                if exc not in excluded:
                    excluded.append(exc)

        return ParsedDirectives(
            cleaned_prompt=text_cleaned.strip(),
            max_words=max_words,
            min_words=min_words,
            history_limit=history_limit,
            excluded_agents=excluded,
            is_file_req=is_file_req,
            target_filename=target_filename,
            is_stop=False,
        )


# Global default instance
directive_parser = DirectiveParser()
