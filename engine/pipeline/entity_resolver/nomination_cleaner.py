"""Pure nomination tag and lead-in stripping utilities (L2)."""

from __future__ import annotations

import re

from config_store.agent_settings import settings_manager
from core.config import AGENTS_CONFIG

LEAD_IN_NOMINATION_PATTERN = (
    r"(?:"
    r"(?:next(?:\s+(?:agent|speaker|turn|persona|up))?\s*(?:is|:|-)?\s*)"
    r"|(?:(?:nomination|nominating|nominate)\s*(?::|-)?\s*)"
    r"|(?:over to(?:\s+you)?\s*,?\s*)"
    r"|(?:(?:handing|passing|turning)(?:\s+over)?\s+to(?:\s+you)?\s*,?\s*)"
    r"|(?:i(?:\s+will|'ll)?\s+nominate\s*(?::|-)?\s*)"
    r"|(?:(?:sljedeći|sljedeci|naredni)(?:\s+(?:agent|govornik))?\s*(?:je|:|-)?\s*)"
    r"|(?:(?:nominacija|nominiram)\s*(?::|-)?\s*)"
    r"|(?:predajem\s+rije[cč](?:\s+za)?\s*,?\s*)"
    r")"
)


def strip_all_nominations(text: str) -> str:
    """
    Completely removes agent nomination tags (including the nominated agent's name
    and any nomination lead-in phrases such as 'Next:', 'Nomination:', 'Over to:', etc.)
    when no agent should be nominated (e.g. force termination or end of panel discussion).
    Any other in-text bracketed agent mentions have their brackets stripped to plain text.
    """
    if not text:
        return text

    from pipeline.entity_resolver.resolver import NOMINATION_TAG_REGEX, entity_resolver

    agents = settings_manager.agents_config or AGENTS_CONFIG
    agent_tokens: set[str] = set(agents.keys())
    for cfg in agents.values():
        if isinstance(cfg, dict):
            uname = cfg.get("username")
            if uname:
                agent_tokens.add(uname)

    for match in NOMINATION_TAG_REGEX.finditer(text):
        raw_tag = match.group(1).strip()
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            agent_tokens.add(raw_tag)
            agent_tokens.add(resolved_key)

    if not agent_tokens:
        return text.strip()

    clean = text.rstrip()
    agent_pattern = r"(?:" + "|".join(re.escape(tok) for tok in sorted(agent_tokens, key=len, reverse=True)) + r")"
    tag_bracket_pattern = r"/\[\s*" + agent_pattern + r"\s*\]"

    lead_in_regex = re.compile(
        r"(?:(?<=[.!?;\n])\s*|\n+|^)\s*"
        + LEAD_IN_NOMINATION_PATTERN
        + tag_bracket_pattern
        + r"(?:\s+next)?(?:\s+to speak)?(?:\s+for (?:your|the) (?:thoughts|review|input))?\s*[.!?]?\s*$",
        re.IGNORECASE | re.DOTALL,
    )

    lead_in_inline_regex = re.compile(
        r"(?:(?<=[.!?;\n])\s*|\n+|^)\s*"
        + LEAD_IN_NOMINATION_PATTERN
        + tag_bracket_pattern
        + r"(?:\s+next)?(?:\s+to speak)?(?:\s+for (?:your|the) (?:thoughts|review|input))?\s*[.!?]?\s*",
        re.IGNORECASE,
    )

    trailing_tag_regex = re.compile(
        r"(?:\n+|\s+)\s*" + tag_bracket_pattern + r"\s*(?:next)?\s*[.!?]?\s*$",
        re.IGNORECASE,
    )

    prev = None
    while prev != clean:
        prev = clean
        clean = lead_in_regex.sub("", clean).rstrip()
        clean = trailing_tag_regex.sub("", clean).rstrip()
        clean = lead_in_inline_regex.sub("", clean).rstrip()

    for tok in sorted(agent_tokens, key=len, reverse=True):
        clean = re.sub(
            r"/\[\s*" + re.escape(tok) + r"\s*\]",
            tok,
            clean,
            flags=re.IGNORECASE,
        )

    return clean.strip()

