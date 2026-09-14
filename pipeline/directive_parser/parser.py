import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.models import ActionDirective, ExtractedIntent, MediaAttachment
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
    """Consolidated container for all parsed directives and skills from a user prompt."""

    cleaned_prompt: str
    actions: list[ActionDirective] = field(default_factory=list)
    modifiers: dict[str, Any] = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)

    # Legacy accessors maintained for full backward compatibility
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
            "actions": [a.to_dict() for a in self.actions],
            "modifiers": dict(self.modifiers),
            "attachments": [att.to_dict() for att in self.attachments],
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
    Unified two-tier parser: deterministic fast-path slash matching + micro-LLM smart merge.
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
        Parses raw text and returns a clean prompt and extracted unified directives.
        """
        # 1. Stop command check (immediate short-circuit)
        if is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm):
            return ParsedDirectives(
                cleaned_prompt=raw_text,
                actions=[ActionDirective(name="stop_discussion")],
                is_stop=True,
            )

        actions: list[ActionDirective] = []
        modifiers: dict[str, Any] = {}

        # 2. Fast-path deterministic extractions (Slash commands)
        text_cleaned = raw_text
        history_limit, text_cleaned = extract_history_limit(text_cleaned)
        max_words, text_cleaned = extract_max_words_limit(text_cleaned)
        min_words, text_cleaned = extract_min_words_limit(text_cleaned)
        excluded, text_cleaned = extract_excluded_agents(text_cleaned)

        is_file_req, target_filename, text_cleaned = extract_file_instruction(
            text_cleaned, available_files=available_files
        )

        if max_words is not None:
            modifiers["max_words"] = max_words
        if min_words is not None:
            modifiers["min_words"] = min_words
        if history_limit is not None:
            modifiers["history"] = history_limit

        if is_file_req:
            actions.append(
                ActionDirective(
                    name="document_reader",
                    params={"target_file": target_filename} if target_filename else {},
                )
            )

        if excluded:
            actions.append(
                ActionDirective(
                    name="exclude_agents",
                    params={"agents": list(excluded)},
                )
            )

        # 3. Dynamic slash check against registered skills
        try:
            from skills import skill_registry

            slash_triggers = skill_registry.get_slash_triggers()
            for slash_cmd, (skill_def, trg_cfg) in slash_triggers.items():
                # If already handled (e.g. by fast-path regexes or previous alias), skip
                if skill_def.name in modifiers or any(a.name == skill_def.name for a in actions):
                    continue

                arg_type = trg_cfg.get("arg_type")
                if arg_type == "list":
                    cmd_match = re.search(rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)", text_cleaned, re.IGNORECASE)
                    if cmd_match:
                        start_idx = cmd_match.start()
                        curr_idx = cmd_match.end()
                        matched_tokens_span_end = curr_idx
                        remainder = text_cleaned[curr_idx:]

                        token_pattern = re.compile(r"(\s*(?:,|and|&)?\s*)(\[[^\]]+\]|[a-zA-Z0-9_-]+)", re.IGNORECASE)
                        offset = 0
                        items_list: list[str] = []
                        known_skill_names = {s.name.lower() for s in skill_registry.list_skills()}

                        while True:
                            t_match = token_pattern.match(remainder[offset:])
                            if not t_match:
                                break
                            _, raw_token = t_match.groups()
                            token_clean = raw_token.strip("[]")
                            rem_after_token = remainder[offset + len(t_match.group(0)):].strip()
                            is_bracketed = raw_token.startswith("[") and raw_token.endswith("]")
                            is_known = token_clean.lower() in known_skill_names or token_clean.lower() == "all"
                            is_last_token = not rem_after_token

                            if is_known or (offset == 0 and is_last_token):
                                items_list.append(token_clean)
                                offset += len(t_match.group(0))
                                matched_tokens_span_end = curr_idx + offset
                            else:
                                break

                        param_name = trg_cfg.get("param_name", "skills" if "skill" in skill_def.name else "items")
                        actions.append(ActionDirective(name=skill_def.name, params={param_name: items_list}))
                        text_cleaned = (text_cleaned[:start_idx] + " " + text_cleaned[matched_tokens_span_end:]).strip()
                        text_cleaned = re.sub(r"[ \t]+", " ", text_cleaned).strip()
                elif arg_type in ("int", "integer"):
                    pattern = re.compile(rf"(?:^|\s){re.escape(slash_cmd)}(?:\s*[:=]?\s*(\d+))?", re.IGNORECASE)
                    match = pattern.search(text_cleaned)
                    if match:
                        num_val = int(match.group(1)) if match.group(1) else None
                        if skill_def.scope == "prompt_modifier":
                            modifiers[skill_def.name] = num_val
                        else:
                            param_name = trg_cfg.get("param_name", "value")
                            actions.append(ActionDirective(name=skill_def.name, params={param_name: num_val}))
                        text_cleaned = re.sub(
                            rf"(?:^|\s){re.escape(slash_cmd)}(?:\s*[:=]?\s*\d+)?",
                            " ",
                            text_cleaned,
                            flags=re.IGNORECASE,
                        ).strip()
                else:
                    pattern = re.compile(rf"(?:^|\s){re.escape(slash_cmd)}(?:\s+([^\s/]+))?", re.IGNORECASE)
                    match = pattern.search(text_cleaned)
                    if match:
                        arg_val = match.group(1)
                        if skill_def.scope == "prompt_modifier":
                            modifiers[skill_def.name] = arg_val or True
                        else:
                            param_name = trg_cfg.get("param_name", "arg")
                            actions.append(ActionDirective(name=skill_def.name, params={param_name: arg_val} if arg_val else {}))
                        text_cleaned = re.sub(
                            rf"(?:^|\s){re.escape(slash_cmd)}(?:\s+[^\s/]+)?",
                            " ",
                            text_cleaned,
                            flags=re.IGNORECASE,
                        ).strip()
        except Exception as e:
            logger.debug(f"[DIRECTIVE PARSER] Skill registry slash check error: {e}")

        # 4. Check if micro-LLM intent extraction should run
        from nlp.intent_extractor import (
            extract_user_intent,
            should_skip_intent_extractor,
        )

        if not should_skip_intent_extractor(raw_text=raw_text) and text_cleaned.strip():
            intent: ExtractedIntent = await extract_user_intent(
                text_cleaned, available_files=available_files
            )
            intent_dict = intent.to_dict() if hasattr(intent, "to_dict") else intent

            # Smart merge: slash commands take absolute priority over LLM inferences
            if max_words is None and intent_dict.get("max_words") is not None:
                max_words = intent_dict["max_words"]
                modifiers["max_words"] = max_words

            if min_words is None and intent_dict.get("min_words") is not None:
                min_words = intent_dict["min_words"]
                modifiers["min_words"] = min_words

            if history_limit is None and intent_dict.get("history") is not None:
                history_limit = intent_dict["history"]
                modifiers["history"] = history_limit

            if not is_file_req and intent_dict.get("file_use"):
                is_file_req = True
                if not target_filename and intent_dict.get("target_file"):
                    target_filename = intent_dict["target_file"]
                if not any(a.name == "document_reader" for a in actions):
                    actions.append(
                        ActionDirective(
                            name="document_reader",
                            params={"target_file": target_filename} if target_filename else {},
                        )
                    )

            for exc in intent_dict.get("excluded_agents", []):
                if exc not in excluded:
                    excluded.append(exc)

            if excluded and not any(a.name == "exclude_agents" for a in actions):
                actions.append(ActionDirective(name="exclude_agents", params={"agents": list(excluded)}))

            # Merge any other modifiers or actions from intent
            llm_modifiers = intent_dict.get("modifiers") or {}
            for mk, mv in llm_modifiers.items():
                if mk not in modifiers:
                    modifiers[mk] = mv

            llm_actions = intent_dict.get("actions") or []
            for act in llm_actions:
                act_name = act.get("name") if isinstance(act, dict) else getattr(act, "name", None)
                act_params = act.get("params", {}) if isinstance(act, dict) else getattr(act, "params", {})
                if act_name and act_name != "stop_discussion" and not any(a.name == act_name for a in actions):
                    actions.append(ActionDirective(name=act_name, params=act_params))

        return ParsedDirectives(
            cleaned_prompt=text_cleaned.strip(),
            actions=actions,
            modifiers=modifiers,
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
