import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.models import ActionDirective, ExtractedIntent, MediaAttachment
from .slash_parser import (
    parse_agent_list,
    parse_file_target,
    parse_grant_skill,
    parse_int_or_off,
    parse_list,
    parse_terminal_flag,
    parse_token,
)

logger = logging.getLogger(__name__)


def is_stop_command(raw_text: str, is_mention_or_dm: bool = False) -> bool:
    """
    Detects if raw_text contains an explicit terminal/stop command.
    Driven dynamically by registered terminal skills from skill_registry.
    """
    if not raw_text:
        return False
    from skills import skill_registry

    return skill_registry.is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm)


@dataclass
class ParsedDirectives:
    """Consolidated container for all parsed directives and skills from a user prompt."""

    cleaned_prompt: str
    actions: list[ActionDirective] = field(default_factory=list)
    modifiers: dict[str, Any] = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)

    # Legacy & domain accessors
    max_words: int | str | None = None
    min_words: int | str | None = None
    scribe_max_words: int | str | None = None
    scribe_min_words: int | str | None = None
    history_limit: int | None = None
    excluded_agents: list[str] = field(default_factory=list)
    included_agents: list[str] = field(default_factory=list)
    exclude_off: bool = False
    include_off: bool = False
    revoke_target: str | None = None
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
            "scribe_max_words": self.scribe_max_words,
            "scribe_min_words": self.scribe_min_words,
            "history_limit": self.history_limit,
            "excluded_agents": self.excluded_agents,
            "included_agents": self.included_agents,
            "exclude_off": self.exclude_off,
            "include_off": self.include_off,
            "revoke_target": self.revoke_target,
            "is_file_req": self.is_file_req,
            "target_filename": self.target_filename,
            "is_stop": self.is_stop,
        }


class DirectiveParser:
    """
    Unified two-tier parser: deterministic dynamic slash matching + micro-LLM smart merge.
    All slash commands and parameters are sourced entirely from the skills registry.
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
        text_cleaned = raw_text

        max_words: int | str | None = None
        min_words: int | str | None = None
        scribe_max_words: int | str | None = None
        scribe_min_words: int | str | None = None
        history_limit: int | None = None
        excluded: list[str] = []
        included: list[str] = []
        exclude_off: bool = False
        include_off: bool = False
        revoke_target: str | None = None
        is_file_req: bool = False
        target_filename: str | None = None

        # 2. Dynamic deterministic slash command extraction driven by SkillRegistry
        try:
            from skills import skill_registry

            slash_triggers = skill_registry.get_slash_triggers()
            sorted_triggers = sorted(
                slash_triggers.items(), key=lambda x: len(x[0]), reverse=True
            )

            for slash_cmd, (skill_def, trg_cfg) in sorted_triggers:
                if skill_def.name in modifiers or any(
                    a.name == skill_def.name for a in actions
                ):
                    continue

                arg_type = trg_cfg.get("arg_type")
                param_name = trg_cfg.get("param_name")

                if arg_type == "terminal_flag" or skill_def.is_terminal:
                    matched, text_cleaned = parse_terminal_flag(
                        slash_cmd, text_cleaned
                    )
                    if matched:
                        actions.append(ActionDirective(name=skill_def.name, params={}))

                elif arg_type in ("int", "integer", "int_or_off"):
                    min_val = 1 if "words" in skill_def.name else None
                    matched, val, text_cleaned = parse_int_or_off(
                        slash_cmd, text_cleaned, min_val=min_val
                    )
                    if matched:
                        if skill_def.scope == "prompt_modifier":
                            modifiers[skill_def.name] = val
                            if skill_def.name == "max_words":
                                max_words = val
                            elif skill_def.name == "min_words":
                                min_words = val
                        elif skill_def.name == "thread_history":
                            history_limit = int(val) if isinstance(val, int) else None
                            modifiers["history"] = history_limit
                        else:
                            p_name = (
                                param_name
                                or ("words" if "words" in skill_def.name else "value")
                            )
                            params_dict: dict[str, Any] = {
                                p_name: val,
                                "slash_cmd": slash_cmd,
                            }
                            if "next" in slash_cmd.lower():
                                params_dict["is_next_round"] = True
                                if val is None:
                                    params_dict[p_name] = 1
                            actions.append(
                                ActionDirective(name=skill_def.name, params=params_dict)
                            )
                            if skill_def.name == "scribe_max_words":
                                scribe_max_words = val
                            elif skill_def.name == "scribe_min_words":
                                scribe_min_words = val

                elif arg_type == "agent_list":
                    matched, agents_list, is_off, text_cleaned = parse_agent_list(
                        slash_cmd, text_cleaned
                    )
                    if matched:
                        actions.append(
                            ActionDirective(
                                name=skill_def.name,
                                params={
                                    param_name or "agents": list(agents_list),
                                    "off": is_off,
                                },
                            )
                        )
                        if skill_def.name == "exclude_agents":
                            excluded = list(agents_list)
                            exclude_off = is_off
                        elif skill_def.name == "include_agents":
                            included = list(agents_list)
                            include_off = is_off

                elif (
                    arg_type in ("file_target", "file")
                    or trg_cfg.get("handles_files")
                    or skill_def.handles_files
                ):
                    matched, target_fn, text_cleaned = parse_file_target(
                        slash_cmd, text_cleaned, available_files=available_files
                    )
                    if matched:
                        is_file_req = True
                        if target_fn:
                            target_filename = target_fn
                        actions.append(
                            ActionDirective(
                                name=skill_def.name,
                                params=(
                                    {param_name or "target_file": target_filename}
                                    if target_filename
                                    else {}
                                ),
                            )
                        )

                elif arg_type in ("token", "str", "string"):
                    matched, target, text_cleaned = parse_token(
                        slash_cmd, text_cleaned
                    )
                    if matched:
                        actions.append(
                            ActionDirective(
                                name=skill_def.name,
                                params={param_name or "target": target},
                            )
                        )
                        if skill_def.name in ("revoke", "revoke_constraint"):
                            revoke_target = target

                elif arg_type == "grant_skill" or skill_def.name == "grant_skill":
                    matched, sk_name, ag_list, text_cleaned = parse_grant_skill(
                        slash_cmd, text_cleaned
                    )
                    if matched:
                        actions.append(
                            ActionDirective(
                                name="grant_skill",
                                params={
                                    "skill": sk_name,
                                    "agents": ag_list,
                                    "skills": [sk_name] if sk_name else [],
                                },
                            )
                        )

                elif arg_type == "list":
                    known_skill_names = {
                        s.name.lower() for s in skill_registry.list_skills()
                    }
                    matched, items_list, text_cleaned = parse_list(
                        slash_cmd, text_cleaned, known_items=known_skill_names
                    )
                    if matched:
                        p_name = param_name or (
                            "skills" if "skill" in skill_def.name else "items"
                        )
                        actions.append(
                            ActionDirective(
                                name=skill_def.name, params={p_name: items_list}
                            )
                        )

                elif arg_type in ("none", "flag"):
                    matched, text_cleaned = parse_terminal_flag(
                        slash_cmd, text_cleaned
                    )
                    if matched:
                        actions.append(ActionDirective(name=skill_def.name, params={}))

                else:
                    # Fallback generic token parser
                    pattern = re.compile(
                        rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)(?:\s*[:=]?\s*([^\s/]+))?",
                        re.IGNORECASE,
                    )
                    match = pattern.search(text_cleaned)
                    if match:
                        arg_val = match.group(1)
                        if skill_def.scope == "prompt_modifier":
                            modifiers[skill_def.name] = arg_val or True
                        else:
                            p_name = param_name or "arg"
                            actions.append(
                                ActionDirective(
                                    name=skill_def.name,
                                    params={p_name: arg_val} if arg_val else {},
                                )
                            )
                        text_cleaned = re.sub(
                            rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)(?:\s*[:=]?\s*[^\s/]+)?",
                            " ",
                            text_cleaned,
                            flags=re.IGNORECASE,
                        ).strip()

            # If no slash command matched file_target, check if available_files filename mentioned in prompt
            if not is_file_req and available_files:
                file_skill = skill_registry.get_file_handler_skill()
                if file_skill:
                    for f in available_files:
                        fname = f.get("name")
                        if fname and re.search(
                            r"\b" + re.escape(fname) + r"\b",
                            text_cleaned,
                            re.IGNORECASE,
                        ):
                            is_file_req = True
                            target_filename = fname
                            actions.append(
                                ActionDirective(
                                    name=file_skill.name,
                                    params={"target_file": target_filename},
                                )
                            )
                            break
        except Exception as e:
            logger.debug(f"[DIRECTIVE PARSER] Skill registry slash check error: {e}")

        # If an exclusive control/grant/revoke/list command is present, skip micro-LLM intent extraction entirely
        has_exclusive_cmd = any(
            a.name in (
                "grant_skill_for_all",
                "grant_skill",
                "revoke_skill_for_all",
                "revoke_constraint",
                "list_skills",
            )
            for a in actions
        )

        # 3. Check if micro-LLM intent extraction should run
        from nlp.intent_extractor import (
            extract_user_intent,
            should_skip_intent_extractor,
        )

        if (
            not has_exclusive_cmd
            and not should_skip_intent_extractor(raw_text=raw_text)
            and text_cleaned.strip()
        ):
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
                from skills import skill_registry

                file_skill = skill_registry.get_file_handler_skill()
                if file_skill and not any(a.name == file_skill.name for a in actions):
                    actions.append(
                        ActionDirective(
                            name=file_skill.name,
                            params=(
                                {"target_file": target_filename}
                                if target_filename
                                else {}
                            ),
                        )
                    )

            from pipeline.entity_resolver import entity_resolver

            tagged_in_prompt = set(entity_resolver.extract_tagged_agents(raw_text))
            for exc in intent_dict.get("excluded_agents", []):
                if exc not in excluded and exc not in tagged_in_prompt:
                    excluded.append(exc)

            if excluded and not any(a.name == "exclude_agents" for a in actions):
                actions.append(
                    ActionDirective(
                        name="exclude_agents", params={"agents": list(excluded)}
                    )
                )

            # Merge any other modifiers or actions from intent
            llm_modifiers = intent_dict.get("modifiers") or {}
            for mk, mv in llm_modifiers.items():
                if mk not in modifiers:
                    modifiers[mk] = mv

            llm_actions = intent_dict.get("actions") or []
            for act in llm_actions:
                act_name = (
                    act.get("name")
                    if isinstance(act, dict)
                    else getattr(act, "name", None)
                )
                act_params = (
                    act.get("params", {})
                    if isinstance(act, dict)
                    else getattr(act, "params", {})
                )
                if (
                    act_name
                    and act_name != "stop_discussion"
                    and not any(a.name == act_name for a in actions)
                ):
                    actions.append(ActionDirective(name=act_name, params=act_params))

        return ParsedDirectives(
            cleaned_prompt=text_cleaned.strip(),
            actions=actions,
            modifiers=modifiers,
            max_words=max_words,
            min_words=min_words,
            scribe_max_words=scribe_max_words,
            scribe_min_words=scribe_min_words,
            history_limit=history_limit,
            excluded_agents=excluded,
            included_agents=included,
            exclude_off=exclude_off,
            include_off=include_off,
            revoke_target=revoke_target,
            is_file_req=is_file_req,
            target_filename=target_filename,
            is_stop=False,
        )


# Global default instance
directive_parser = DirectiveParser()
