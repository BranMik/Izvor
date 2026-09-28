import asyncio
import json
import logging
import re
from enum import Enum
from typing import Any, Final

import litellm

from core.llm_utils import execute_llm_with_retry
from core.models import ActionDirective, ExtractedIntent
from pipeline.directive_parser import is_stop_command
from pipeline.entity_resolver import entity_resolver

logger = logging.getLogger(__name__)


class OrchestratorTask(str, Enum):
    """Execution tasks supported by the Orchestrator Agent."""

    INTENT_EXTRACTION = "intent_extraction"
    LANGUAGE_DETECTION = "language_detection"
    SYSTEM_MODERATOR = "system_moderator"
    THREAD_NOTES = "thread_notes"
    PRICING_RESEARCH = "pricing_research"


# Normalization map for canonical English language names
LANGUAGE_NAME_MAP: Final[dict[str, str]] = {
    "croatian": "Croatian",
    "hrvatski": "Croatian",
    "bosnian": "Bosnian",
    "bosanski": "Bosnian",
    "serbian": "Serbian",
    "srpski": "Serbian",
    "english": "English",
    "engleski": "English",
    "german": "German",
    "deutsch": "German",
    "njemački": "German",
    "njemacki": "German",
    "spanish": "Spanish",
    "español": "Spanish",
    "espanol": "Spanish",
    "španjolski": "Spanish",
    "spanjolski": "Spanish",
    "french": "French",
    "français": "French",
    "francais": "French",
    "francuski": "French",
    "italian": "Italian",
    "italiano": "Italian",
    "talijanski": "Italian",
    "portuguese": "Portuguese",
    "português": "Portuguese",
    "portugalski": "Portuguese",
}

DEFAULT_INTENT_SYSTEM_PROMPT = """You are a precise, language-agnostic intent, directive, and parameter extractor for an AI multi-agent Slack platform.
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

DEFAULT_LANGUAGE_SYSTEM_PROMPT = (
    "You are an ultra-fast, accurate language classifier. "
    "Identify the primary natural language of the user's message. "
    "Output ONLY the canonical language name in English as a single word (e.g. Croatian, Bosnian, Serbian, English, German, Spanish, French, Italian). "
    "Do NOT output any punctuation, reasoning, or markdown. "
    "If the message is predominantly non-English but contains English technical terms, library/app names, or buzzwords, identify the surrounding natural language (e.g. Croatian), NOT English. "
    "Only output English if the natural text itself is in English, or if it is purely code/data with no human language."
)


class OrchestratorAgent:
    """
    Unified Orchestrator Agent for SlackAgenticPanel.
    Combines pre-turn intake/intent extraction, language detection, Slack moderator responses,
    and post-turn thread synthesis using task-phase driven dynamic prompt and tool injection.
    Leverages fast_model for low-latency tasks and reasoning_model for deep synthesis.
    """

    def __init__(
        self,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        username: str = "Orchestrator",
        icon_url: str = "static/image_30.png",
    ):
        self._custom_fast_model = fast_model
        self._custom_reasoning_model = reasoning_model
        self.username = username
        self.icon_url = icon_url

    @property
    def fast_model(self) -> str:
        if self._custom_fast_model:
            return self._custom_fast_model
        try:
            from orchestration.agent_settings import settings_manager
            return settings_manager.get_orchestrator_fast_model()
        except (ImportError, AttributeError, KeyError):
            return "gemini/gemini-3.8-flash"

    @property
    def reasoning_model(self) -> str:
        if self._custom_reasoning_model:
            return self._custom_reasoning_model
        try:
            from orchestration.agent_settings import settings_manager
            return settings_manager.get_orchestrator_reasoning_model()
        except (ImportError, AttributeError, KeyError):
            return "mistral/mistral-large-latest"

    def get_model_for_task(self, task: OrchestratorTask) -> str:
        """Selects either the fast_model or reasoning_model based on task complexity."""
        if task == OrchestratorTask.THREAD_NOTES:
            return self.reasoning_model
        return self.fast_model

    def build_prompt_for_task(self, task: OrchestratorTask, **kwargs: Any) -> str:
        """
        Dynamically constructs the system prompt tailored strictly to the execution task.
        Prevents prompt bloat and instruction confusion.
        """
        if task == OrchestratorTask.INTENT_EXTRACTION:
            try:
                from skills import skill_registry
                catalog = skill_registry.build_extractor_catalog_prompt()
                if catalog:
                    return catalog
            except (ImportError, AttributeError, KeyError, TypeError) as e:
                logger.debug(f"[ORCHESTRATOR] Error retrieving skill catalog prompt: {e}")
            return DEFAULT_INTENT_SYSTEM_PROMPT

        if task == OrchestratorTask.LANGUAGE_DETECTION:
            return DEFAULT_LANGUAGE_SYSTEM_PROMPT

        if task == OrchestratorTask.THREAD_NOTES:
            try:
                from prompts import build_agent_prompt
                return build_agent_prompt("Thread_Notes_Agent")
            except (ImportError, AttributeError, KeyError, TypeError) as e:
                logger.debug(f"[ORCHESTRATOR] Error retrieving thread notes prompt: {e}")
                return "You are the Live Discussion Scribe synthesizing multi-agent discussions into concise notes."

        if task == OrchestratorTask.SYSTEM_MODERATOR:
            return (
                "You are the System Moderator for the CaiLogics multi-agent discussion platform. "
                "Provide clear, professional, markdown-formatted announcements or status updates to users."
            )

        if task == OrchestratorTask.PRICING_RESEARCH:
            try:
                from prompts import build_agent_prompt
                return build_agent_prompt("Pricing_Agent")
            except (ImportError, AttributeError, KeyError, TypeError) as e:
                logger.debug(f"[ORCHESTRATOR] Error retrieving pricing prompt: {e}")
                return "You are the Model Pricing Research Agent. Return accurate per-token pricing."

        return "You are the Orchestrator Agent."

    def get_tools_for_task(self, task: OrchestratorTask) -> list[dict[str, Any]]:
        """
        Dynamically binds only the tools required for the specific task phase.
        Ensures zero tool bloat on high-frequency, latency-critical tasks.
        """
        if task in (OrchestratorTask.INTENT_EXTRACTION, OrchestratorTask.LANGUAGE_DETECTION):
            return []

        if task == OrchestratorTask.PRICING_RESEARCH:
            try:
                from skills import skill_registry
                web_skill = skill_registry.get_skill("web_research")
                if web_skill:
                    return web_skill.get_tool_schemas()
            except (ImportError, AttributeError, KeyError):
                pass
            return []

        return []

    def should_skip_intent(self, raw_text: str, **kwargs: Any) -> bool:
        """Determines if intent extraction should be skipped (e.g. /stop)."""
        return is_stop_command(raw_text)

    async def extract_intent(
        self,
        text: str,
        available_files: list[dict[str, Any]] | None = None,
        timeout_seconds: float = 3.0,
    ) -> ExtractedIntent:
        """
        Extracts structured intent parameters using fast_model.
        Returns an ExtractedIntent instance.
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
        system_prompt = self.build_prompt_for_task(OrchestratorTask.INTENT_EXTRACTION)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        model = self.get_model_for_task(OrchestratorTask.INTENT_EXTRACTION)

        try:
            resp = await asyncio.wait_for(
                execute_llm_with_retry(
                    func=litellm.completion,
                    kwargs={
                        "model": model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "num_retries": 1,
                    },
                    agent_key="orchestrator",
                    thread_key=("system", "orchestrator_intent"),
                    max_attempts=2,
                ),
                timeout=timeout_seconds,
            )

            if not resp or not getattr(resp, "choices", None):
                return default_intent

            content = resp.choices[0].message.content
            if not content:
                return default_intent

            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                return default_intent

            extracted_actions: list[ActionDirective] = []
            for item in data.get("actions", []):
                if isinstance(item, dict) and "name" in item:
                    action_name = str(item["name"])
                    params = item.get("params", {})
                    if not isinstance(params, dict):
                        params = {}
                    if action_name in ("exclude_agents", "include_agents"):
                        for param_key in ("agents", "agent_names"):
                            if param_key in params:
                                raw_val = params[param_key]
                                names = raw_val if isinstance(raw_val, list) else [str(raw_val)]
                                resolved = [
                                    entity_resolver.resolve_agent_name(n)
                                    for n in names
                                    if entity_resolver.resolve_agent_name(n)
                                ]
                                params[param_key] = resolved

                    extracted_actions.append(ActionDirective(name=action_name, params=params))

            modifiers = data.get("modifiers", {})
            if not isinstance(modifiers, dict):
                modifiers = {}

            return ExtractedIntent(
                modifiers=modifiers,
                actions=extracted_actions,
            )

        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"[ORCHESTRATOR] Intent extraction fallback: {e}")
            return default_intent

    async def detect_language(self, text: str, timeout_seconds: float = 3.0) -> str:
        """
        Classifies user prompt natural language using fast_model.
        Returns canonical capitalized language name (e.g. 'English', 'Croatian').
        """
        if not text or not text.strip():
            return "English"

        model = self.get_model_for_task(OrchestratorTask.LANGUAGE_DETECTION)
        system_prompt = self.build_prompt_for_task(OrchestratorTask.LANGUAGE_DETECTION)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        try:
            resp = await asyncio.wait_for(
                execute_llm_with_retry(
                    func=litellm.completion,
                    kwargs={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 10,
                        "temperature": 0.0,
                        "num_retries": 1,
                    },
                    agent_key="orchestrator",
                    thread_key=("system", "orchestrator_language"),
                    max_attempts=2,
                ),
                timeout=timeout_seconds,
            )

            if not resp or not getattr(resp, "choices", None):
                return "English"

            content = resp.choices[0].message.content or ""
            clean_word = content.strip().split("\n")[0].split()
            if clean_word:
                first = re.sub(r"[^\w]", "", clean_word[0]).lower()
                if first in LANGUAGE_NAME_MAP:
                    return LANGUAGE_NAME_MAP[first]
                return clean_word[0].capitalize()

            return "English"

        except Exception as e:
            logger.debug(f"[ORCHESTRATOR] Language detection fallback: {e}")
            return "English"


orchestrator_agent = OrchestratorAgent()
