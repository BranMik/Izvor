import asyncio
import json
import logging
import re
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from core.config import INTENT_EXTRACTOR_MODEL
from core.llm_utils import execute_llm_with_retry
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
        **kwargs: Any,
    ) -> bool:
        """
        Evaluates whether the LLM intent extractor should be skipped:
        Only /stop command triggers skip as True.
        All other prompts proceed to intent extraction.
        Legacy keyword arguments are accepted via kwargs for backward compatibility.
        """
        return is_stop_command(raw_text)

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
            resp = await asyncio.wait_for(
                execute_llm_with_retry(
                    func=litellm.completion,
                    kwargs={
                        "model": self.model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "num_retries": 1,
                    },
                    agent_key="intent_extractor",
                    thread_key=("system", "intent_extractor"),
                    max_attempts=2,
                ),
                timeout=timeout_seconds,
            )

            if not resp or not getattr(resp, "choices", None):
                return default_intent

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
                        # stop_discussion is strictly a deterministic /stop slash command, never inferred by LLM
                        if act_name == "stop_discussion":
                            continue
                        act_params = dict(act.get("params") or {})
                        if act_name == "exclude_agents":
                            raw_agents = act_params.get("agents", [])
                            if isinstance(raw_agents, list):
                                resolved_agents = []
                                for ag in raw_agents:
                                    res = resolve_agent_name(str(ag))
                                    if res and res not in resolved_agents:
                                        resolved_agents.append(res)
                                act_params["agents"] = resolved_agents
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

            if excluded_agents:
                exc_actions = [a for a in actions if a.name == "exclude_agents"]
                if exc_actions:
                    existing = exc_actions[0].params.get("agents", [])
                    merged = list(existing)
                    for a in excluded_agents:
                        if a not in merged:
                            merged.append(a)
                    exc_actions[0].params["agents"] = merged
                else:
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
    **kwargs: Any,
) -> bool:
    return intent_extractor.should_skip(raw_text=raw_text, **kwargs)


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
