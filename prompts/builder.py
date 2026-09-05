import json
import re
from pathlib import Path
from functools import lru_cache

PROMPTS_DIR = Path(__file__).resolve().parent
PERSONAS_DIR = PROMPTS_DIR / "personas"
SHARED_DIR = PROMPTS_DIR / "shared"


@lru_cache(maxsize=None)
def _read_prompt_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _load_short_output_agents() -> set[str]:
    """Loads agent keys configured with is_short_output: true from agent_settings.json."""
    settings_path = PROMPTS_DIR.parent / "data" / "agent_settings.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                k for k, v in data.get("agents", {}).items()
                if isinstance(v, dict) and v.get("is_short_output") is True
            }
        except Exception:
            pass
    return set()


# Agents restricted to a concise 100 words hard limit (loaded from agent_settings.json)
SHORT_OUTPUT_AGENTS: set[str] = _load_short_output_agents()


def _load_system_agents() -> set[str]:
    """Loads agent keys configured as system agents from agent_settings.json metadata."""
    settings_path = PROMPTS_DIR.parent / "data" / "agent_settings.json"
    system_keys: set[str] = set()
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for sys_k, sys_v in data.get("system_agents", {}).items():
                if isinstance(sys_v, dict):
                    system_keys.add(sys_k)
                    if sys_v.get("agent_key"):
                        system_keys.add(str(sys_v["agent_key"]))
                elif isinstance(sys_k, str):
                    system_keys.add(sys_k)
            for k, v in data.get("agents", {}).items():
                if isinstance(v, dict) and v.get("is_system_agent") is True:
                    system_keys.add(k)
        except Exception:
            pass
    return system_keys


# System agents with self-contained prompts (loaded from agent_settings.json metadata)
SYSTEM_AGENTS: set[str] = _load_system_agents()


def is_system_agent(agent_key: str) -> bool:
    """Checks whether an agent is configured as a system agent via metadata or settings."""
    if agent_key in SYSTEM_AGENTS:
        return True

    # Runtime fallback lookup against core.config.SYSTEM_AGENTS_CONFIG
    try:
        import sys

        core_cfg = sys.modules.get("core.config")
        if core_cfg and hasattr(core_cfg, "SYSTEM_AGENTS_CONFIG"):
            sys_cfg = getattr(core_cfg, "SYSTEM_AGENTS_CONFIG", {})
            if agent_key in sys_cfg:
                return True
            for k, entry in sys_cfg.items():
                if isinstance(entry, dict):
                    if entry.get("agent_key") == agent_key:
                        return True
                    if k == agent_key and entry.get("is_system_agent") is True:
                        return True
    except Exception:
        pass

    return False


def _format_word_constraints(
    max_words: int | None = None, min_words: int | None = None
) -> str:
    constraints = []
    if min_words is not None:
        constraints.append(
            f"Minimum Length: Your entire response must have a minimum length of {min_words} words."
        )
    if max_words is not None:
        constraints.append(
            f"Hard Limit: Your entire response must strictly NOT exceed {max_words} words."
        )
    return "\n".join(constraints)


def build_agent_prompt(
    agent_key: str,
    max_words: int | None = None,
    min_words: int | None = None,
    skills: list[str] | None = None,
) -> str:
    """
    Builds the complete system prompt for a given agent by combining
    its persona definition with shared constraints, nomination rules,
    and equipped skills guidelines where appropriate.

    If max_words or min_words is specified:
    - Completely skips adding output_constraints.md / output_constraints_100w.md text.
    - Adds only the simple constraint for max_words and/or min_words.
    """
    persona_file = PERSONAS_DIR / f"{agent_key}.md"
    persona_content = _read_prompt_file(persona_file)

    # Support agents with self-contained prompts driven by metadata
    if is_system_agent(agent_key):
        return f"\n{persona_content}\n\n"

    word_constraints = _format_word_constraints(
        max_words=max_words, min_words=min_words
    )

    nomination_rules = _read_prompt_file(SHARED_DIR / "nomination_rules.md")
    fixed_constraints = _read_prompt_file(SHARED_DIR / "fixed_constraints.md")

    # All debate agents share fixed constraints, output constraints (100w or 200w), and nomination rules
    if word_constraints:
        output_constraints = (
            f"OUTPUT & LENGTH CONSTRAINTS:\n\n"
            f"{word_constraints}"
        )
    elif agent_key in SHORT_OUTPUT_AGENTS:
        output_constraints = _read_prompt_file(SHARED_DIR / "output_constraints_100w.md")
    else:
        output_constraints = _read_prompt_file(SHARED_DIR / "output_constraints.md")

    prompt = (
        f"\n{persona_content}\n\n"
        f"{fixed_constraints}\n\n"
        f"{output_constraints}\n\n"
        f"{nomination_rules}\n\n"
    )

    # Append Skills Catalog for on-demand progressive disclosure (strictly filtered to authorized skills)
    try:
        from skills import skill_registry

        effective_skills = skills
        if effective_skills is None:
            try:
                from core.config import AGENTS_CONFIG
                effective_skills = AGENTS_CONFIG.get(agent_key, {}).get("skills", [])
            except Exception:
                effective_skills = []

        # Only inject catalog for skills this agent is authorized to load
        catalog_prompt = skill_registry.get_skills_catalog_prompt(allowed_skills=effective_skills)
        if catalog_prompt:
            prompt += f"\n{catalog_prompt}\n"

        if effective_skills:
            pinned_guidelines = []
            for s_name in effective_skills:
                s_def = skill_registry.get(s_name)
                if s_def and s_def.instructions:
                    pinned_guidelines.append(s_def.instructions)
            if pinned_guidelines:
                prompt += f"\n### PRE-EQUIPPED SKILL GUIDELINES\n" + "\n\n".join(pinned_guidelines) + "\n\n"
    except Exception as e:
        logger.warning(f"[PROMPT BUILDER] Failed to load skills catalog/guidelines for {agent_key}: {e}")

    return prompt
