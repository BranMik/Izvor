"""Deterministic agent recommendation and missing-skill resolution hints."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _load_agent_selection_context(
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> tuple[dict[str, Any], set[str], set[str], dict[str, list[str]]]:
    """Loads configured agents, enabled keys, active workflow keys, and thread agent grants."""
    from core.config import AGENTS_CONFIG

    agents_cfg: dict[str, Any] = AGENTS_CONFIG
    enabled_agents: set[str] = set()
    try:
        from config_store.agent_settings import settings_manager

        agents_cfg = settings_manager.agents_config or AGENTS_CONFIG
        enabled_agents = settings_manager.get_enabled_agents()
    except (ImportError, AttributeError, TypeError):
        agents_cfg = AGENTS_CONFIG
        enabled_agents = set()

    workflow_agents: set[str] = set()
    try:
        from core.registry import get_initial_agents_from_provider

        workflow_agents = set(get_initial_agents_from_provider(channel_id, thread_ts) or [])
    except (ImportError, AttributeError, KeyError, TypeError, RuntimeError):
        workflow_agents = set()

    thread_agent_skills: dict[str, list[str]] = {}
    if channel_id and thread_ts:
        try:
            from services.thread_skills_store import thread_skills_manager

            thread_agent_skills = thread_skills_manager.get_all_active_agent_skills(channel_id, thread_ts)
        except (ImportError, AttributeError, KeyError, TypeError):
            thread_agent_skills = {}

    return agents_cfg, enabled_agents, workflow_agents, thread_agent_skills


def select_recommended_agent_from_keys(
    candidate_keys: Iterable[str],
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    exclude_agent_key: str | None = None,
    coverage_counts: dict[str, int] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    enabled_agents: set[str] | None = None,
    workflow_agents: set[str] | None = None,
) -> str | None:
    """
    Selects a single recommended agent username from candidate agent keys.
    Priority: enabled > in active workflow > skill coverage > alphabetical username > agent key.
    """
    keys_list = list(candidate_keys or ())
    if not keys_list:
        return None

    if agents_cfg is None or enabled_agents is None or workflow_agents is None:
        loaded_cfg, loaded_enabled, loaded_wf, _ = _load_agent_selection_context(
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if agents_cfg is None:
            agents_cfg = loaded_cfg
        if enabled_agents is None:
            enabled_agents = loaded_enabled
        if workflow_agents is None:
            workflow_agents = loaded_wf

    ranked: list[tuple[int, int, int, str, str, str]] = []
    for raw_key in keys_list:
        agent_key = str(raw_key).strip()
        if not agent_key or (exclude_agent_key and agent_key == exclude_agent_key):
            continue
        cfg = agents_cfg.get(agent_key)
        if isinstance(cfg, dict):
            username = str(cfg.get("username") or agent_key).strip()
        else:
            username = agent_key
        is_enabled = agent_key in enabled_agents
        is_in_workflow = agent_key in workflow_agents
        coverage = coverage_counts.get(agent_key, 1) if coverage_counts is not None else 1
        ranked.append(
            (
                0 if is_enabled else 1,
                0 if is_in_workflow else 1,
                -coverage,
                username.casefold(),
                agent_key,
                username,
            )
        )

    if not ranked:
        return None
    ranked.sort()
    return ranked[0][5]


def recommend_agent_for_skills(
    skill_names: list[str] | str,
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    exclude_agent_key: str | None = None,
) -> str | None:
    """Selects 1 recommended agent username authorized for the requested skill(s)."""
    if isinstance(skill_names, str):
        skill_list = [skill_names.strip()] if skill_names.strip() else []
    else:
        skill_list = [str(s).strip() for s in skill_names if str(s).strip()]

    if not skill_list:
        return None

    agents_cfg, enabled_agents, workflow_agents, thread_agent_skills = _load_agent_selection_context(
        channel_id=channel_id,
        thread_ts=thread_ts,
    )

    coverage_counts: dict[str, int] = {}
    for agent_key, cfg in agents_cfg.items():
        if not isinstance(cfg, dict):
            continue
        if exclude_agent_key and agent_key == exclude_agent_key:
            continue
        agent_skill_set = set(cfg.get("skills", []) or []) | set(thread_agent_skills.get(agent_key, []) or [])
        coverage = sum(1 for s in skill_list if s in agent_skill_set)
        if coverage > 0:
            coverage_counts[agent_key] = coverage

    if not coverage_counts:
        return None

    return select_recommended_agent_from_keys(
        coverage_counts.keys(),
        channel_id=channel_id,
        thread_ts=thread_ts,
        exclude_agent_key=exclude_agent_key,
        coverage_counts=coverage_counts,
        agents_cfg=agents_cfg,
        enabled_agents=enabled_agents,
        workflow_agents=workflow_agents,
    )


def get_skill_resolution_hint(
    skill_names: list[str] | str,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    exclude_agent_key: str | None = None,
) -> str:
    """
    Generates a concise 1-line resolution hint recommending 1 authorized agent by username
    (if any) and how to grant the skill via /grant_skill or /grant_skill_for_all.
    """
    if isinstance(skill_names, str):
        raw_list = [skill_names.strip()]
    else:
        raw_list = [str(s).strip() for s in skill_names if str(s).strip()]

    skill_list = list(dict.fromkeys(s for s in raw_list if s))
    if not skill_list:
        return "Grant access via '/grant_skill <skill> <agent>' or '/grant_skill_for_all <skill>'."

    rec_username = recommend_agent_for_skills(
        skill_list,
        channel_id=channel_id,
        thread_ts=thread_ts,
        exclude_agent_key=exclude_agent_key,
    )

    skills_fmt = ", ".join(f"'{s}'" for s in skill_list)
    if len(skill_list) == 1:
        s_name = skill_list[0]
        grant_clause = f"'/grant_skill {s_name} <agent>' or '/grant_skill_for_all {s_name}'"
    else:
        grant_clause = "'/grant_skill <skill> <agent>' or '/grant_skill_for_all <skill>'"

    if rec_username:
        return f"Recommended agent for {skills_fmt}: {rec_username} — or grant via {grant_clause}."
    return f"Grant {skills_fmt} via {grant_clause}."

