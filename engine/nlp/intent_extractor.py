import asyncio
import inspect
import json
import logging
from typing import Any

import litellm

from core.config import get_intent_extractor_model
from core.llm_utils import execute_llm_with_retry
from core.models import INT_MODIFIER_BOUNDS, ActionDirective, ExtractedIntent, coerce_int_modifier
from pipeline.directive_parser import is_stop_command
from pipeline.entity_resolver import entity_resolver

from prompts.builder import load_system_prompt

logger = logging.getLogger(__name__)

_DEFAULT_EXTRACTOR_PROMPT_FALLBACK = """<system_agent_specification id="Intent_Extractor" role="Structured Directive & Parameter Extractor">
  <task_objective>
    Analyze incoming user messages in a language-agnostic manner and extract all requested formatting constraints, prompt modifiers, and action triggers into a strict JSON payload.
  </task_objective>

  <processing_rules>
    1. Extract "modifiers": Formatting rules, word count limits (`max_words`, `min_words`), or context constraints requested by the user as key-value pairs.
    2. Extract "actions": Operations, skills, or control directives requested by the user, each with `"name"` and `"params"`.
    3. Extract "language": The language of the user's message (e.g. "Croatian", "English", "German", "Spanish", etc.).
    4. Output strictly valid JSON with zero markdown wrappers, explanations, or conversational text.
  </processing_rules>

  <output_contract>
    Respond ONLY with a valid JSON object matching this exact schema:
    {
      "language": "<detected_language_name_or_code>",
      "modifiers": {
        "<modifier_name>": <value>
      },
      "actions": [
        { "name": "<skill_or_action_name>", "params": { ... } }
      ]
    }
  </output_contract>
</system_agent_specification>"""

EXTRACTOR_SYSTEM_PROMPT = load_system_prompt("Intent_Extractor", fallback=_DEFAULT_EXTRACTOR_PROMPT_FALLBACK)

_INT_MODIFIER_BOUNDS = INT_MODIFIER_BOUNDS


_extractor_override: Any = None


def configure_intent_extractor(fn: Any) -> None:
    """Configures an in-memory intent extractor override for tests and DI."""
    global _extractor_override
    _extractor_override = fn


def reset_intent_extractor() -> None:
    """Resets the in-memory intent extractor override."""
    global _extractor_override
    _extractor_override = None


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

    def should_skip(self, raw_text: str) -> bool:
        """
        Evaluates whether the LLM intent extractor should be skipped:
        Only /stop command triggers skip as True.
        """
        return is_stop_command(raw_text)

    async def extract_intent(
        self,
        text: str,
        available_files: list[dict[str, Any]] | None = None,
        timeout_seconds: float | None = None,
        thread_key: tuple[str, str] | None = None,
    ) -> ExtractedIntent:
        """
        Calls extractor model to parse structured constraints (max_words, history, file_use, target_file, excluded_agents).
        Returns an ExtractedIntent dataclass instance.

        On LLM timeout the call is re-run once with an escalated timeout
        (timeout_seconds * system_agent_inference_retry_timeout_multiplier) before falling back.
        """
        from orchestration.system_settings import (
            get_system_agent_inference_retry_timeout_multiplier,
            get_system_agent_inference_timeout_seconds,
        )

        if timeout_seconds is None:
            timeout_seconds = get_system_agent_inference_timeout_seconds()
        retry_multiplier = get_system_agent_inference_retry_timeout_multiplier()

        if _extractor_override is not None:
            res = _extractor_override(text, available_files=available_files, timeout_seconds=timeout_seconds, thread_key=thread_key)
            if inspect.isawaitable(res):
                return await res
            return res

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
            escalated_timeout = timeout_seconds * retry_multiplier
            for attempt_timeout in (timeout_seconds, escalated_timeout):
                try:
                    resp = await asyncio.wait_for(
                        execute_llm_with_retry(
                            func=litellm.acompletion,
                            kwargs={
                                "model": self.model,
                                "messages": messages,
                                "response_format": {"type": "json_object"},
                                "num_retries": 1,
                            },
                            agent_key="intent_extractor",
                            thread_key=thread_key or ("system", "intent_extractor"),
                            max_attempts=2,
                        ),
                        timeout=attempt_timeout,
                    )
                    break
                except (TimeoutError, asyncio.TimeoutError):
                    logger.warning(
                        f"[INTENT EXTRACTOR] Extraction attempt timed out after {attempt_timeout:.1f}s."
                    )
            else:
                logger.warning(
                    f"[INTENT EXTRACTOR] Extraction timed out even after escalation to {escalated_timeout:.1f}s. Falling back."
                )
                return self._deterministic_fallback(text)

            if not resp or not getattr(resp, "choices", None):
                return default_intent

            if resp and getattr(resp, "usage", None) and thread_key and thread_key != ("system", "intent_extractor"):
                try:
                    from agents.usage_agent import record_thread_tokens

                    p_tok = getattr(resp.usage, "prompt_tokens", 0) or 0
                    c_tok = getattr(resp.usage, "completion_tokens", 0) or 0
                    if p_tok or c_tok:
                        record_thread_tokens(
                            channel_id=thread_key[0],
                            thread_ts=thread_key[1],
                            model_name=self.model,
                            prompt_tokens=p_tok,
                            completion_tokens=c_tok,
                        )
                except Exception as ue:  # noqa: BLE001
                    logger.debug(f"[INTENT EXTRACTOR] Error recording token spend: {ue}")

            content = resp.choices[0].message.content
            if not content:
                return default_intent

            parsed = json.loads(content)

            actions: list[ActionDirective] = []
            modifiers: dict[str, Any] = {}

            # 1. Modifiers from structured "modifiers" sub-dict (int modifiers coerced + clamped)
            raw_mods = parsed.get("modifiers")
            if isinstance(raw_mods, dict):
                for k, v in raw_mods.items():
                    if v is None:
                        continue
                    bounds = _INT_MODIFIER_BOUNDS.get(k)
                    if bounds is not None:
                        coerced = coerce_int_modifier(v, bounds[0], bounds[1])
                        if coerced is not None:
                            modifiers[k] = coerced
                    else:
                        modifiers[k] = v

            from core.registry import get_skill_registry

            skill_registry = get_skill_registry()
            allowed_inferred_skills = {s.name for s in skill_registry.get_auto_detect_skills()} | {"exclude_agents"}

            # 2. Actions from structured "actions" list
            raw_actions = parsed.get("actions")
            if isinstance(raw_actions, list):
                for act in raw_actions:
                    if isinstance(act, dict) and act.get("name"):
                        act_name = str(act["name"]).strip()
                        # Strictly allowlist inferred actions: only auto_detect skills (never grant/revoke/rounds/stop)
                        if act_name not in allowed_inferred_skills:
                            logger.warning(
                                f"[INTENT EXTRACTOR] Dropping disallowed inferred action '{act_name}' (not an auto-detect skill)"
                            )
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
                        actions.append(ActionDirective(name=act_name, params=act_params, origin="inferred"))

            # 3. Dynamic file handler resolution

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

            detected_lang = parsed.get("language")
            if detected_lang and isinstance(detected_lang, str):
                detected_lang = detected_lang.strip() or None
            else:
                detected_lang = None

            logger.info(
                f"[INTENT EXTRACTOR] Extracted: language={detected_lang}, modifiers={modifiers}, actions={[a.name for a in actions]}"
            )

            return ExtractedIntent(
                actions=actions,
                modifiers=modifiers,
                detected_language=detected_lang,
            )

        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(f"[INTENT EXTRACTOR] Extraction timed out after {timeout_seconds}s. Falling back.")
            return self._deterministic_fallback(text)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"[INTENT EXTRACTOR] Extraction parsing error ({e}). Falling back.")
            return self._deterministic_fallback(text)
        except Exception as e:  # noqa: BLE001 - Intent extraction error boundary falls back cleanly to default intent
            logger.warning(f"[INTENT EXTRACTOR] Extraction failed ({e}). Falling back.")
            return self._deterministic_fallback(text)

    def _deterministic_fallback(self, text: str) -> ExtractedIntent:
        """Deterministic fallback when LLM times out or extraction fails."""
        return ExtractedIntent(actions=[], modifiers={})


# Default singleton instance
intent_extractor = IntentExtractor()


async def extract_intent(
    text: str,
    available_files: list[dict[str, Any]] | None = None,
    timeout_seconds: float | None = None,
    thread_key: tuple[str, str] | None = None,
) -> ExtractedIntent:
    """Module-level convenience delegating to default intent_extractor instance."""
    return await intent_extractor.extract_intent(
        text,
        available_files=available_files,
        timeout_seconds=timeout_seconds,
        thread_key=thread_key,
    )
