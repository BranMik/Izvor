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


def build_agent_prompt(agent_key: str) -> str:
    """
    Builds the complete system prompt for a given agent by combining
    its persona definition with shared skills (output constraints & nomination rules)
    where appropriate.
    """
    persona_file = PERSONAS_DIR / f"{agent_key}.md"
    persona_content = _read_prompt_file(persona_file)

    # Gemini_Visionary, OpAiTerra_Architect, Thread_Notes_Agent, and Usage_Agent have self-contained specialized prompts
    if agent_key in (
        "Gemini_Visionary",
        "OpAiTerra_Architect",
        "Thread_Notes_Agent",
        "Usage_Agent",
    ):
        return f"\n{persona_content}\n\n"

    # All intermediate agents share output constraints and nomination rules
    output_constraints = _read_prompt_file(SHARED_DIR / "output_constraints.md")
    nomination_rules = _read_prompt_file(SHARED_DIR / "nomination_rules.md")

    return (
        f"\n{persona_content}\n\n"
        f"{output_constraints}\n"
        f"{nomination_rules}\n\n"
    )
