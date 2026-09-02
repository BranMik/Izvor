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
) -> str:
    """
    Builds the complete system prompt for a given agent by combining
    its persona definition with shared skills (output constraints & nomination rules)
    where appropriate.

    If max_words or min_words is specified:
    - Completely skips adding output_constraints.md / output_constraints_100w.md text.
    - Adds only the simple constraint for max_words and/or min_words.
    """
    persona_file = PERSONAS_DIR / f"{agent_key}.md"
    persona_content = _read_prompt_file(persona_file)

    # Support agents with self-contained prompts
    if agent_key in (
        "Thread_Notes_Agent",
        "Usage_Agent",
        "Pricing_Agent",
    ):
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

    return (
        f"\n{persona_content}\n\n"
        f"{fixed_constraints}\n\n"
        f"{output_constraints}\n\n"
        f"{nomination_rules}\n\n"
    )
