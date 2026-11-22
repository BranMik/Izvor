import logging
import re
from functools import cache
from pathlib import Path
from typing import Any

from core.paths import (
    PERSONAS_DIR,
    PROMPTS_DIR,
    SYSTEM_AGENTS_DIR,
)

logger = logging.getLogger(__name__)

SHARED_DIR = PROMPTS_DIR / "shared"


@cache
def _read_prompt_file_cached(path_str: str, mtime_ns: int) -> str:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _read_prompt_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    mtime_ns = path.stat().st_mtime_ns
    return _read_prompt_file_cached(str(path.resolve()), mtime_ns)


@cache
def _load_system_prompt_cached(path_str: str, mtime_ns: int, fallback: str) -> str:
    file_path = Path(path_str)
    if file_path.exists():
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"Error reading prompt file {file_path}: {e}")
    return fallback.strip()


def load_system_prompt(name: str, fallback: str = "") -> str:
    """Loads a system agent prompt markdown file from SYSTEM_AGENTS_DIR with caching and fallback."""
    file_path = SYSTEM_AGENTS_DIR / f"{name}.md"
    if file_path.exists():
        mtime_ns = file_path.stat().st_mtime_ns
        return _load_system_prompt_cached(str(file_path.resolve()), mtime_ns, fallback)
    return fallback.strip()


def get_short_output_agents() -> set[str]:
    """Dynamically returns agent keys configured with is_short_output: true from settings_manager."""
    try:
        from orchestration.agent_settings import settings_manager
        cfg = settings_manager.get_agents_config()
        if cfg:
            return {
                k for k, v in cfg.items()
                if isinstance(v, dict) and v.get("is_short_output") is True
            }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PROMPT BUILDER] Error retrieving short output agents: {e}")
    return set()


DIALECTICAL_SYSTEM_AGENTS = {"Challenger", "Arbitrator"}


def is_dialectical_system_agent(agent_key: str) -> bool:
    """Checks whether an agent is a turn-taking dialectical system agent."""
    return agent_key in DIALECTICAL_SYSTEM_AGENTS


def is_short_output_agent(agent_key: str) -> bool:
    """Checks whether an agent is configured with is_short_output: true dynamically from SSOT."""
    try:
        from orchestration.agent_settings import settings_manager
        agent_cfg = settings_manager.get_agent(agent_key)
        if agent_cfg and agent_cfg.get("is_short_output") is True:
            return True
        cfg = settings_manager.get_agents_config().get(agent_key)
        if cfg and cfg.get("is_short_output") is True:
            return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PROMPT BUILDER] Dynamic short output check fallback: {e}")
    return agent_key in get_short_output_agents()


def get_system_agents() -> set[str]:
    """Dynamically queries system agents from settings_manager / core.config."""
    system_keys: set[str] = {"orchestrator", "Orchestrator"}
    try:
        from orchestration.agent_settings import settings_manager
        for k, v in settings_manager.system_agents_config.items():
            system_keys.add(k)
            if isinstance(v, dict) and v.get("agent_key"):
                system_keys.add(str(v["agent_key"]))
        for k, v in settings_manager.get_agents_config().items():
            if isinstance(v, dict) and v.get("is_system_agent") is True:
                system_keys.add(k)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PROMPT BUILDER] Error loading system agents: {e}")
    return system_keys


def is_system_agent(agent_key: str) -> bool:
    """Checks whether an agent is configured as a system agent via metadata or settings."""
    if agent_key in ("orchestrator", "Orchestrator"):
        return True
    try:
        from orchestration.agent_settings import settings_manager
        if settings_manager.is_system_agent(agent_key):
            return True
        agent_cfg = settings_manager.get_agent(agent_key)
        if agent_cfg and agent_cfg.get("is_system_agent") is True:
            return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PROMPT BUILDER] Dynamic system agent check fallback: {e}")

    if agent_key in get_system_agents():
        return True

    # Runtime fallback lookup against core.config.SYSTEM_AGENTS_CONFIG
    try:
        from core.config import SYSTEM_AGENTS_CONFIG
        if agent_key in SYSTEM_AGENTS_CONFIG:
            return True
        for k, entry in SYSTEM_AGENTS_CONFIG.items():
            if isinstance(entry, dict):
                if entry.get("agent_key") == agent_key:
                    return True
                if k == agent_key and entry.get("is_system_agent") is True:
                    return True
    except (ImportError, AttributeError, KeyError, TypeError) as e:
        logger.debug(f"[PROMPT BUILDER] Runtime system agent lookup skipped: {e}")

    return False


COMPACT_WORD_THRESHOLD = 60
_OUTPUT_CONTRACT_RE = re.compile(r"\s*<output_contract>[\s\S]*?</output_contract>\s*", re.IGNORECASE)


def _strip_output_contract(persona_content: str) -> str:
    """Removes the <output_contract> section when a tight word limit overrides multi-section formatting."""
    return _OUTPUT_CONTRACT_RE.sub("\n", persona_content).strip()


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


def _resolve_configured_max_words(
    use_short: bool,
    default_max_words: int | None = None,
    short_max_words: int | None = None,
) -> int:
    """Resolves the configured default or short output word limit clamped to INT_MODIFIER_BOUNDS['max_words']."""
    from core.models import INT_MODIFIER_BOUNDS

    min_b, max_b = INT_MODIFIER_BOUNDS["max_words"]
    override = short_max_words if use_short else default_max_words
    if override is not None:
        return max(min_b, min(max_b, int(override)))
    try:
        from orchestration.system_settings import get_output_max_words

        return max(min_b, min(max_b, int(get_output_max_words(is_short=use_short))))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PROMPT BUILDER] Falling back to default output_constraints limit: {e}")
        return 100 if use_short else 200


def _build_output_constraints(
    agent_key: str,
    max_words: int | None = None,
    min_words: int | None = None,
    is_short_output: bool | None = None,
    default_max_words: int | None = None,
    short_max_words: int | None = None,
) -> tuple[str, bool]:
    """
    Constructs the <output_and_length_constraints> block and returns (block, is_compact_mode).
    Uses system_settings output_constraints limits when max_words/min_words are not explicitly set.
    When the active max_words (explicit or configured) < COMPACT_WORD_THRESHOLD, switches to compact
    mode (omits 3-part bullets and signals that persona <output_contract> formatting is overridden).
    When max_words >= COMPACT_WORD_THRESHOLD or only min_words is set, preserves shared structural
    rules (Rules 2-4) while replacing Rule 1 with the active bounds.
    """
    use_short = is_short_output if is_short_output is not None else is_short_output_agent(agent_key)
    base_filename = "output_constraints_short.md" if use_short else "output_constraints.md"
    base_constraints = _read_prompt_file(SHARED_DIR / base_filename)

    if max_words is not None:
        effective_max: int | None = max_words
    elif min_words is not None:
        effective_max = None
    else:
        effective_max = _resolve_configured_max_words(
            use_short=use_short,
            default_max_words=default_max_words,
            short_max_words=short_max_words,
        )

    word_constraints = _format_word_constraints(max_words=effective_max, min_words=min_words)
    if not word_constraints:
        return base_constraints, False

    if effective_max is not None and effective_max < COMPACT_WORD_THRESHOLD:
        compact_block = (
            "<output_and_length_constraints>\n"
            "OUTPUT & LENGTH CONSTRAINTS:\n\n"
            f"{word_constraints}\n"
            "Compact Override: Omit all multi-section headings, bullet lists, and conversational preamble; "
            "deliver a single concise response strictly within the word limit.\n"
            "Skill-Lack Notice Compression: Keep any missing-skill or file-permission notice strictly <= 50 words "
            "(including a 1-line hint recommending just 1 authorized agent by name—never by agent ID—or /grant_skill) with zero reflection on skills architecture.\n"
            "</output_and_length_constraints>"
        )
        return compact_block, True

    updated_constraints = re.sub(
        r"^1\.\s*Hard Limit:.*$",
        f"1. {word_constraints}",
        base_constraints,
        count=1,
        flags=re.MULTILINE,
    )
    return updated_constraints, False


def build_agent_prompt(
    agent_key: str,
    max_words: int | None = None,
    min_words: int | None = None,
    skills: list[str] | None = None,
    modifiers: dict[str, Any] | None = None,
    is_short_output: bool | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    default_max_words: int | None = None,
    short_max_words: int | None = None,
) -> str:
    """
    Builds the complete system prompt for a given agent by combining
    its persona definition with shared constraints, nomination rules,
    and equipped skills guidelines where appropriate.

    If max_words or min_words is specified:
    - Uses output_constraints.md / output_constraints_short.md Rules 2-4 with updated Rule 1 bounds
      (or switches to compact mode and strips persona <output_contract> when max_words < COMPACT_WORD_THRESHOLD).
    """
    persona_file_sys = SYSTEM_AGENTS_DIR / f"{agent_key}.md"
    if persona_file_sys.exists():
        persona_content = _read_prompt_file(persona_file_sys)
        if not is_dialectical_system_agent(agent_key):
            return f"\n{persona_content}\n\n"
    else:
        persona_file = PERSONAS_DIR / f"{agent_key}.md"
        persona_content = _read_prompt_file(persona_file)

        # Support agents with self-contained prompts driven by metadata
        if is_system_agent(agent_key) and not is_dialectical_system_agent(agent_key):
            return f"\n{persona_content}\n\n"

    effective_max = max_words
    effective_min = min_words
    if modifiers:
        mod_max = modifiers.get("max_words")
        if effective_max is None and isinstance(mod_max, int) and not isinstance(mod_max, bool):
            effective_max = mod_max
        mod_min = modifiers.get("min_words")
        if effective_min is None and isinstance(mod_min, int) and not isinstance(mod_min, bool):
            effective_min = mod_min

    output_constraints, is_compact_mode = _build_output_constraints(
        agent_key,
        max_words=effective_max,
        min_words=effective_min,
        is_short_output=is_short_output,
        default_max_words=default_max_words,
        short_max_words=short_max_words,
    )
    if is_compact_mode:
        persona_content = _strip_output_contract(persona_content)

    nomination_rules = _read_prompt_file(SHARED_DIR / "nomination_rules.md")
    fixed_constraints = _read_prompt_file(SHARED_DIR / "fixed_constraints.md")

    # Render any custom prompt modifiers from skills
    custom_directives = []
    if modifiers:
        try:
            from core.registry import get_skill_registry
            skill_registry = get_skill_registry()

            for mod_k, mod_v in modifiers.items():
                if mod_k in ("max_words", "min_words", "history", "file_use", "target_file", "excluded_agents"):
                    continue
                sk = skill_registry.get(mod_k)
                if sk and sk.instructions:
                    formatted_inst = sk.instructions.replace("{value}", str(mod_v))
                    custom_directives.append(formatted_inst)
                elif mod_v:
                    logger.warning(
                        f"[PROMPT BUILDER] Ignoring unverified prompt modifier key '{mod_k}' (not a registered skill with instructions)"
                    )
        except (ImportError, AttributeError, KeyError, TypeError, ValueError, RuntimeError) as e:
            logger.debug(f"[PROMPT BUILDER] Error formatting custom modifiers: {e}")

    directives_block = f"\n\n{chr(10).join(custom_directives)}\n" if custom_directives else ""

    prompt = (
        f"\n{persona_content}\n\n"
        f"{fixed_constraints}\n\n"
        f"{output_constraints}\n\n"
        f"{nomination_rules}\n"
        f"{directives_block}\n"
    )

    # Append workspace baseline context if configured
    try:
        from orchestration.app_settings import get_workspace_context
        ws_ctx = get_workspace_context()
        if ws_ctx:
            prompt += f"\n### WORKSPACE BASELINE CONTEXT\n{ws_ctx}\n\n"
    except (ImportError, AttributeError) as e:
        logger.debug(f"[PROMPT BUILDER] Could not load workspace context: {e}")

    # Append Skills Directory and on-demand progressive disclosure (strictly filtered to authorized skills)
    try:
        from core.registry import get_skill_registry
        try:
            skill_registry = get_skill_registry()
        except RuntimeError:
            import importlib
            importlib.import_module("skills.loader")
            skill_registry = get_skill_registry()

        # Inject comprehensive skills directory so any agent asked can accurately list all skills and authorized agents
        skills_dir_prompt = skill_registry.get_skills_directory_prompt(
            channel_id=channel_id, thread_ts=thread_ts
        )
        if skills_dir_prompt:
            prompt += f"\n{skills_dir_prompt}\n"

        effective_skills = skills
        if effective_skills is None:
            try:
                from orchestration.agent_settings import get_agents_config

                effective_skills = get_agents_config().get(agent_key, {}).get("skills", [])
            except (ImportError, AttributeError, KeyError, TypeError):
                from core.config import AGENTS_CONFIG

                effective_skills = AGENTS_CONFIG.get(agent_key, {}).get("skills", [])

        # Only inject catalog for skills this agent is authorized to load
        catalog_prompt = skill_registry.get_skills_catalog_prompt(allowed_skills=effective_skills)
        if catalog_prompt:
            prompt += f"\n{catalog_prompt}\n"
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, RuntimeError) as e:
        logger.warning(f"[PROMPT BUILDER] Failed to load skills catalog/guidelines for {agent_key}: {e}")

    return prompt
