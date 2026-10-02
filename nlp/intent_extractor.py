import asyncio
import json
import logging
from typing import Any

import litellm

from core.config import get_intent_extractor_model
from core.llm_utils import execute_llm_with_retry
from core.models import ActionDirective, ExtractedIntent
from pipeline.directive_parser import is_stop_command
from pipeline.entity_resolver import entity_resolver

logger = logging.getLogger(__name__)

EXTRACTOR_SYSTEM_PROMPT = """You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.
Analyze the user's message and extract any requested constraints, directives, or action triggers:

1. "modifiers": Formatting, word count limits, or context constraints requested by the user (key-value pairs).
2. "actions": Operations, tools, or directives requested by the user.

Respond ONLY with a valid JSON object matching this schema:
{
  "modifiers": {
    "<modifier_name>": <value>
  },
  "actions": [
    { "name": "<skill_or_action_name>", "params": { ... } }
  ]
}"""


class IntentExtractor:
    """Extracts structured intent parameters using LLM in a language-agnostic manner."""

    def __init__(
        self,
        model: str | None = None,
        system_prompt: str | None = None,
    ):
        self._custom_model = model
        self._custom_system_prompt = system_prompt

    @property
    def model(self) -> str:
        return self._custom_model or get_intent_extractor_model()

    @model.setter
    def model(self, val: str | None) -> None:
        self._custom_model = val

    @property
    def system_prompt(self) -> str:
        if self._custom_system_prompt:
            return self._custom_system_prompt
        try:
            from core.registry import get_skill_registry
            skill_registry = get_skill_registry()
            prompt = skill_registry.build_extractor_catalog_prompt()
            if prompt:
                return prompt
        except (ImportError, AttributeError, KeyError, TypeError) as e:
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

            # 2. Actions from structured "actions" list
            raw_actions = parsed.get("actions")
            if isinstance(raw_actions, list):
                for act in raw_actions:
                    if isinstance(act, dict) and act.get("name"):
                        act_name = str(act["name"]).strip()
                        # stop_discussion and rounds are strictly deterministic slash commands, never inferred by LLM
                        if act_name in ("stop_discussion", "rounds"):
                            continue
                        act_params = dict(act.get("params") or {})
                        if act_name == "exclude_agents":
                            raw_agents = act_params.get("agents", [])
                            if isinstance(raw_agents, list):
                                resolved_agents = []
                                for ag in raw_agents:
                                    res = entity_resolver.resolve_agent_name(str(ag))
                                    if res and res not in resolved_agents:
                                        resolved_agents.append(res)
                                act_params["agents"] = resolved_agents
                        actions.append(ActionDirective(name=act_name, params=act_params))

            # 3. Dynamic file handler resolution
            from core.registry import get_skill_registry
            skill_registry = get_skill_registry()

            file_skill = skill_registry.get_file_handler_skill()
            if file_skill:
                file_skill_name = file_skill.name
                target_file = parsed.get("target_file")
                if target_file and isinstance(target_file, str):
                    target_file = target_file.strip() or None
                else:
                    target_file = None

                # If LLM returned file action or target file, ensure params are set
                file_acts = [a for a in actions if a.name == file_skill_name]
                if file_acts and target_file and not file_acts[0].params.get("target_file"):
                    file_acts[0].params["target_file"] = target_file

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
            return self._deterministic_fallback(text)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"[INTENT EXTRACTOR] Extraction parsing error ({e}). Falling back.")
            return self._deterministic_fallback(text)
        except Exception as e:  # noqa: BLE001 - Intent extraction error boundary falls back cleanly to default intent
            logger.warning(f"[INTENT EXTRACTOR] Extraction failed ({e}). Falling back.")
            return self._deterministic_fallback(text)

    def _deterministic_fallback(self, text: str) -> ExtractedIntent:
        """Deterministic regex fallback for explicitly tagged agents when LLM times out."""
        actions = []
        try:
            from pipeline.entity_resolver import entity_resolver
            tagged = entity_resolver.extract_tagged_agents(text)
            for ag in tagged:
                resolved = entity_resolver.resolve_agent_name(ag)
                if resolved:
                    actions.append(ActionDirective(name=resolved, params={}))
        except Exception as e:
            logger.warning(f"[INTENT EXTRACTOR] Deterministic fallback error: {e}")
            
        return ExtractedIntent(actions=actions, modifiers={})


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
