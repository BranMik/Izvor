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


# Agents restricted to a concise 100 words hard limit
SHORT_OUTPUT_AGENTS = {
    "Gemini_DataAndAnalyticsStrategist",
    "Gemini_ScalabilityEngineer",
    "MistLar_CompliancePrivacyAndGovernanceOfficer",
    "MistLar_DeploymentReleaseStrategist",
    "MistLar_MonitoringAndAlertingSpecialist",
    "MistLar_OperationalReliabilityLead",
    "MistLar_QALead",
    "MistLar_ScopeOptimizer",
}


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

    if agent_key == "Gemini_Visionary":
        if word_constraints:
            return (
                f"\n{persona_content}\n\n"
                f"OUTPUT & LENGTH CONSTRAINTS:\n\n"
                f"{word_constraints}\n\n"
            )
        return f"\n{persona_content}\n\n"

    if agent_key == "OpAiTerra_Architect":
        content = persona_content
        if max_words is not None:
            content = re.sub(
                r"strictly NOT exceed \d+ words",
                f"strictly NOT exceed {max_words} words",
                content,
            )
        if min_words is not None:
            content = (
                f"{content}\n"
                f"7. Minimum Length: Your entire response must have a minimum length of {min_words} words."
            )
        return f"\n{content}\n\n"

    # Intermediate agents share output constraints (100 words or 200 words) and nomination rules
    if word_constraints:
        output_constraints = (
            f"OUTPUT & LENGTH CONSTRAINTS:\n\n"
            f"{word_constraints}"
        )
    elif agent_key in SHORT_OUTPUT_AGENTS:
        output_constraints = _read_prompt_file(SHARED_DIR / "output_constraints_100w.md")
    else:
        output_constraints = _read_prompt_file(SHARED_DIR / "output_constraints.md")

    nomination_rules = _read_prompt_file(SHARED_DIR / "nomination_rules.md")

    return (
        f"\n{persona_content}\n\n"
        f"{output_constraints}\n"
        f"{nomination_rules}\n\n"
    )

