import asyncio
import json
import logging
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from core.config import INTENT_EXTRACTOR_MODEL
from core.models import ActionDirective, ExtractedIntent

from .nl_patterns import is_stop_command, resolve_agent_name

logger = logging.getLogger(__name__)

EXTRACTOR_SYSTEM_PROMPT = """You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.
Analyze the user's message and extract any requested constraints or directives:

1. "max_words": Integer maximum word count if the user requested a maximum length ceiling.
2. "min_words": Integer minimum word count if the user requested a minimum length floor.
3. "history": Integer number of previous messages to consider.
4. "document_reader": If user asks to read, analyze, inspect, or reference an attached file/document.
5. "exclude_agents": List of agent names if the user explicitly requests to exclude agents.

Respond ONLY with a valid JSON object matching this schema:
{
  "modifiers": {
    "max_words": <int or null>,
    "min_words": <int or null>,
    "history": <int or null>
  },
  "actions": [
    { "name": "<skill_name>", "params": { ... } }
  ]
}"""


class IntentExtractor:
    """Extracts structured intent parameters using LLM in a language-agnostic manner."""

    def __init__(
        self,
        model: str = INTENT_EXTRACTOR_MODEL,
        system_prompt: str | None = None,
    ):
        self.model = model
        self._custom_system_prompt = system_prompt

    @property
    def system_prompt(self) -> str:
        if self._custom_system_prompt:
            return self._custom_system_prompt
        try:
            from skills import skill_registry
            prompt = skill_registry.build_extractor_catalog_prompt()
            if prompt:
                return prompt
        except Exception as e:
            logger.debug(f"[INTENT EXTRACTOR] Error retrieving skill catalog prompt: {e}")
        return EXTRACTOR_SYSTEM_PROMPT

    def should_skip(
        self,
        raw_text: str,
        is_file_cmd: bool = False,
        has_slash_max_words: bool = False,
        has_slash_history: bool = False,
        has_thread_files: bool = False,
    ) -> bool:
        """
        Evaluates whether the LLM intent extractor should be skipped:
        1. Always skip if /stop command is present.
        2. Never skip just because /file command is present alone.
        3. Do not skip if there is /history or /max_words AND a file is attached to the thread (without /file).
        4. Skip if /file is called explicitly AND either /max_words or /history (or both) is also called explicitly.
        5. Skip if either /max_words or /history is called explicitly and no thread files exist.
        """
        if is_stop_command(raw_text):
            return True

        if is_file_cmd and (has_slash_max_words or has_slash_history):
            return True

        return bool((has_slash_max_words or has_slash_history) and not has_thread_files)

    async def extract_intent(
        self,
        text: str,
        available_files: list[dict[str, Any]] | None = None,
        timeout_seconds: float = 3.0,
    ) -> ExtractedIntent:
        """
        Calls extractor model to parse structured constraints (max_words, history, file_use, target_file, excluded_agents).
        Returns an ExtractedIntent dataclass instance.
        """
        default_intent = ExtractedIntent()

        if not text or not text.strip():
            return default_intent

        file_hint = ""
        if available_files:
            filenames = [f.get("name") for f in available_files if f.get("name")]
            if filenames:
                file_hint = f"\n(Files currently available in thread: {', '.join(filenames)})"

        user_content = f"User message: {text}{file_hint}"

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            def _call_llm():
                return litellm.completion(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    num_retries=1,
                )

            resp = await asyncio.wait_for(
                run_in_threadpool(_call_llm),
                timeout=timeout_seconds,
            )

            content = resp.choices[0].message.content
            if not content:
                return default_intent

            parsed = json.loads(content)

            actions: list[ActionDirective] = []
            modifiers: dict[str, Any] = {}

            # 1. Modifiers from structured "modifiers" sub-dict
            raw_mods = parsed.get("modifiers")
            if isinstance(raw_mods, dict):
                for k, v in raw_mods.items():
                    if v is not None:
                        modifiers[k] = v

            # Fallback/merge legacy top-level keys
            raw_max = parsed.get("max_words") or modifiers.get("max_words")
            if raw_max is not None and str(raw_max).isdigit() and int(raw_max) > 0:
                modifiers["max_words"] = int(raw_max)

            raw_min = parsed.get("min_words") or modifiers.get("min_words")
            if raw_min is not None and str(raw_min).isdigit() and int(raw_min) > 0:
                modifiers["min_words"] = int(raw_min)

            raw_hist = parsed.get("history") or modifiers.get("history")
            if raw_hist is not None and str(raw_hist).isdigit() and int(raw_hist) >= 0:
                modifiers["history"] = int(raw_hist)

            # 2. Actions from structured "actions" list
            raw_actions = parsed.get("actions")
            if isinstance(raw_actions, list):
                for act in raw_actions:
                    if isinstance(act, dict) and act.get("name"):
                        act_name = str(act["name"]).strip()
                        act_params = dict(act.get("params") or {})
                        actions.append(ActionDirective(name=act_name, params=act_params))

            # Legacy file_use / target_file
            file_use = bool(parsed.get("file_use", False)) or any(a.name == "document_reader" for a in actions)
            target_file = parsed.get("target_file")
            if isinstance(target_file, str):
                target_file = target_file.strip() or None
            else:
                target_file = None

            if file_use and not any(a.name == "document_reader" for a in actions):
                actions.append(ActionDirective(name="document_reader", params={"target_file": target_file} if target_file else {}))
            elif target_file and any(a.name == "document_reader" for a in actions):
                doc_acts = [a for a in actions if a.name == "document_reader"]
                if doc_acts and not doc_acts[0].params.get("target_file"):
                    doc_acts[0].params["target_file"] = target_file

            # Legacy excluded_agents
            raw_excluded = parsed.get("excluded_agents", [])
            excluded_agents = []
            if isinstance(raw_excluded, list):
                for agent_token in raw_excluded:
                    if isinstance(agent_token, str):
                        resolved = resolve_agent_name(agent_token)
                        if resolved and resolved not in excluded_agents:
                            excluded_agents.append(resolved)

            if excluded_agents and not any(a.name == "exclude_agents" for a in actions):
                actions.append(ActionDirective(name="exclude_agents", params={"agents": excluded_agents}))

            logger.info(
                f"[INTENT EXTRACTOR] Extracted: modifiers={modifiers}, actions={[a.name for a in actions]}"
            )

            return ExtractedIntent(
                actions=actions,
                modifiers=modifiers,
            )

        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                f"[INTENT EXTRACTOR] Extraction timed out after {timeout_seconds}s. Falling back."
            )
            return default_intent
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"[INTENT EXTRACTOR] Extraction parsing error ({e}). Falling back.")
            return default_intent
        except Exception as e:
            logger.warning(f"[INTENT EXTRACTOR] Extraction failed ({e}). Falling back.")
            return default_intent


# Default singleton instance
intent_extractor = IntentExtractor()


# Backward-compatible function aliases
def should_skip_intent_extractor(
    raw_text: str,
    is_file_cmd: bool = False,
    has_slash_max_words: bool = False,
    has_slash_history: bool = False,
    has_thread_files: bool = False,
) -> bool:
    return intent_extractor.should_skip(
        raw_text=raw_text,
        is_file_cmd=is_file_cmd,
        has_slash_max_words=has_slash_max_words,
        has_slash_history=has_slash_history,
        has_thread_files=has_thread_files,
    )


async def extract_user_intent(
    text: str,
    available_files: list[dict[str, Any]] | None = None,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    intent_obj = await intent_extractor.extract_intent(
        text=text,
        available_files=available_files,
        timeout_seconds=timeout_seconds,
    )
    return intent_obj.to_dict()
