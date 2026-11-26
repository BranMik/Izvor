import asyncio
import json
import logging
import os
import re
from typing import Any

import litellm

from core.config import (
    SYSTEM_AGENTS_CONFIG,
    get_orchestrator_fast_model,
    get_orchestrator_reasoning_model,
    get_system_agent_model,
)
from core.llm_utils import execute_llm_with_retry
from core.security_utils import wrap_untrusted_content
from orchestration.state.evidence_store import READ_ONLY_CACHEABLE_TOOLS
from config_store.system_settings import get_shared_evidence_max_chars
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext
from prompts.builder import build_agent_prompt, render_prompt_template
from services.slack import post_agent_response

logger = logging.getLogger(__name__)

CONTROL_PLANE_TOOLS: frozenset[str] = frozenset({
    "load_skill",
    "nominate_next_speaker",
    "request_user_clarification",
    "signal_consensus_convergence",
})

_UNTRUSTED_TAG_RE = re.compile(
    r"<\s*/?\s*untrusted_file_content(?:\s+[^>\"']*(?:\"[^\"]*\"|'[^']*')*)*\s*/?>|"
    r"<\s*/?\s*untrusted_file_content[^>]*>",
    re.IGNORECASE,
)

_VERIFIER_BOUNDARY_TAG_RE = re.compile(
    r"<\s*/?\s*(?:authoritative_ground_truth|initial_user_prompt|retrieved_evidence|"
    r"evidence_verification_directive|verification_scope|cited_prior_statements|"
    r"agent_statement|target_message_to_verify|stage1_preliminary_flag|"
    r"confirmation_instructions)(?:\s+[^>]*)?>",
    re.IGNORECASE,
)

_MAX_CITED_STATEMENT_CHARS = 1200
_REVISOR_SPEAKER_NAMES: frozenset[str] = frozenset({"internal revisor", "internalrevisor"})


def _speaker_aliases(speaker: str) -> list[str]:
    """Builds deduplicated case-insensitive name aliases (length >= 3) for a speaker."""
    raw = str(speaker or "").strip()
    if not raw:
        return []
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", raw).strip()
    spaceless = re.sub(r"\s+", "", raw).strip()
    candidates: list[str] = []
    for base in (raw, spaced, spaceless):
        if base:
            candidates.append(base)
            if base.lower().startswith("the ") and len(base) > 4:
                candidates.append(base[4:].strip())
    seen: set[str] = set()
    aliases: list[str] = []
    for alias in candidates:
        low = alias.lower()
        if len(alias) >= 3 and low not in seen:
            seen.add(low)
            aliases.append(alias)
    return aliases


def _extract_prior_speaker_statements(assistant_msgs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extracts cleaned (speaker, body) pairs from prior assistant messages, skipping Internal Revisor."""
    extracted: list[tuple[str, str]] = []
    for msg in assistant_msgs or []:
        if not isinstance(msg, dict):
            continue
        raw = str(msg.get("content", "")).strip()
        match = re.match(r"^\[([^\]]+)\]:", raw)
        if not match:
            continue
        speaker = match.group(1).strip()
        if not speaker or speaker.lower() in _REVISOR_SPEAKER_NAMES:
            continue
        cleaned = _clean_candidate_message(raw)
        if cleaned:
            extracted.append((speaker, cleaned))
    return extracted


def _select_cited_prior_statements(
    cleaned_message: str,
    current_speaker: str,
    prior_turns: list[tuple[str, str]],
    max_chars_per_speaker: int = _MAX_CITED_STATEMENT_CHARS,
) -> list[tuple[str, str]]:
    """
    Deterministically selects prior statements only for speakers explicitly referenced
    by name/alias in `cleaned_message` (excluding `current_speaker`).
    """
    if not cleaned_message or not prior_turns:
        return []

    current_key = str(current_speaker or "").strip().lower()
    grouped: dict[str, tuple[str, list[str]]] = {}
    for speaker, text in prior_turns:
        sp_key = speaker.strip().lower()
        if not sp_key or sp_key == current_key:
            continue
        if sp_key not in grouped:
            grouped[sp_key] = (speaker, [])
        grouped[sp_key][1].append(text)

    cited: list[tuple[str, str]] = []
    for canonical_speaker, statements in grouped.values():
        aliases = _speaker_aliases(canonical_speaker)
        if not any(
            re.search(rf"\b{re.escape(alias)}\b", cleaned_message, re.IGNORECASE)
            for alias in aliases
        ):
            continue

        # Prioritize most recent turns within budget when truncating
        selected_chunks: list[str] = []
        used_chars = 0
        was_truncated = False
        for stmt in reversed(statements):
            clean_stmt = _strip_verifier_boundary_tags(stmt).strip()
            if not clean_stmt:
                continue
            sep_len = 2 if selected_chunks else 0
            if used_chars + sep_len + len(clean_stmt) <= max_chars_per_speaker:
                selected_chunks.append(clean_stmt)
                used_chars += sep_len + len(clean_stmt)
            else:
                remaining_budget = max_chars_per_speaker - used_chars - sep_len
                if remaining_budget > 20:
                    selected_chunks.append(
                        clean_stmt[: max(0, remaining_budget - 15)].rstrip() + "... [truncated]"
                    )
                was_truncated = True
                break

        if not selected_chunks:
            continue
        selected_chunks.reverse()
        combined = "\n\n".join(selected_chunks)
        if was_truncated and len(combined) > max_chars_per_speaker:
            combined = combined[: max(0, max_chars_per_speaker - 15)].rstrip() + "... [truncated]"
        cited.append((canonical_speaker, combined))

    return cited


def _strip_verifier_boundary_tags(text: str) -> str:
    """Strips XML boundary tags used by the revisor prompt to prevent delimiter injection."""
    if not text:
        return ""
    return _VERIFIER_BOUNDARY_TAG_RE.sub("", str(text))


def _clean_candidate_message(raw_text: str) -> str:
    """Strips duplicate speaker headers, nomination tags, skill footers, and token footers."""
    if not raw_text:
        return ""
    cleaned = str(raw_text).strip()
    # Remove one or more leading [Speaker]: prefixes
    while True:
        updated = re.sub(r"^\[[^\]]+\]:\s*", "", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated

    # Iteratively strip trailing orchestration footers (token footer, fallback footer, Skills used, nomination tags)
    while True:
        prev = cleaned
        cleaned = re.sub(r"\s*\{(?:[^{}]+:\s*)?\d+/\d+\}\s*$", "", cleaned).strip()
        cleaned = re.sub(
            r"\s*⚠️\s*_Fallback model used:\s*`[^`]+`\s*\(missing API key for\s*`[^`]+`\)_\s*$",
            "",
            cleaned,
        ).strip()
        cleaned = re.sub(
            r"\s*Skills used:\s*(?:\[[^\]]+\](?:,\s*)?)+\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*_?Using source extracted by\s+[^\r\n_]+?\.?_?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*/\[[^\]]+\](?:\s+next)?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == prev:
            break

    return _strip_verifier_boundary_tags(cleaned).strip()


def _is_pure_missing_skill_notice(ctx: TurnContext) -> bool:
    """Checks if the turn was a compressed missing-skill refusal with no tool execution."""
    if getattr(ctx.output, "is_deterministic_missing_skill", False):
        return True
    missing = getattr(ctx, "missing_required_skills", None)
    if not missing:
        return False
    if getattr(ctx.output, "skills_used", None):
        return False
    if getattr(ctx, "shared_evidence_source_agents", None):
        return False
    final_text = _clean_candidate_message(getattr(ctx.output, "final_text", "") or "")
    words = final_text.split()
    return len(words) <= 55


def _format_bounded_evidence(label: str, raw_body: str, max_chars: int) -> str:
    """Unwraps nested untrusted tags, enforces max_chars ceiling, and wraps content safely."""
    body = _UNTRUSTED_TAG_RE.sub("", str(raw_body or "")).strip()
    body = _strip_verifier_boundary_tags(body)
    if len(body) > max_chars:
        body = body[: max(0, max_chars - 15)] + "... [truncated]"
    wrapped = wrap_untrusted_content(body)
    return f"{label}\n{wrapped}"


def _collect_ground_truth_evidence(ctx: TurnContext) -> tuple[str, bool]:
    """Collects, truncates, and wraps verified file/tool/round evidence for the thread."""
    retrieved_parts: list[str] = []
    max_chars = get_shared_evidence_max_chars()

    file_ctx = ctx.file_context or thread_state_manager.get_file_context(
        ctx.input.channel_id, ctx.input.thread_ts
    )
    if file_ctx:
        if isinstance(file_ctx, dict):
            f_name = (
                file_ctx.get("file_name")
                or file_ctx.get("filename")
                or file_ctx.get("name")
                or "Document"
            )
            f_body = (
                file_ctx.get("content")
                or file_ctx.get("text")
                or json.dumps(file_ctx, indent=2)
            )
            if str(f_body).strip():
                retrieved_parts.append(
                    _format_bounded_evidence(
                        f"DOCUMENT/RAG CONTEXT ({f_name}):", str(f_body), max_chars
                    )
                )
        elif str(file_ctx).strip():
            retrieved_parts.append(
                _format_bounded_evidence(
                    "DOCUMENT/RAG CONTEXT:", str(file_ctx), max_chars
                )
            )

    for msg in (getattr(ctx, "messages", None) or []):
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tool_name = msg.get("name") or "tool"
            if tool_name in CONTROL_PLANE_TOOLS:
                continue
            tool_content = msg.get("content", "")
            if tool_content and str(tool_content).strip():
                retrieved_parts.append(
                    _format_bounded_evidence(
                        f"TOOL OUTPUT [{tool_name}]:", str(tool_content), max_chars
                    )
                )

    evidence_items = thread_state_manager.get_round_evidence(
        ctx.input.channel_id, ctx.input.thread_ts
    )
    seen_evidence: set[tuple[str, str, str]] = set()
    for ev in evidence_items:
        if ev.scope_key in ("*", ctx.agent_key) and ev.tool_name in READ_ONLY_CACHEABLE_TOOLS:
            ev_key = (ev.tool_name, ev.scope_key, getattr(ev, "args_hash", ""))
            if ev_key in seen_evidence:
                continue
            seen_evidence.add(ev_key)
            ev_res = str(ev.result).strip()
            if not ev_res:
                continue
            ev_body = ev_res
            if ev_res.startswith("{") and ev_res.endswith("}"):
                try:
                    parsed = json.loads(ev_res)
                    if isinstance(parsed, dict) and "content" in parsed and parsed["content"]:
                        doc_name = parsed.get("document") or parsed.get("file") or ev.tool_name
                        ev_body = f"DOCUMENT ({doc_name}):\n{parsed['content']}"
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            retrieved_parts.append(
                _format_bounded_evidence(
                    f"VERIFIED EVIDENCE [{ev.tool_name} by {ev.agent_key}]:",
                    ev_body,
                    max_chars,
                )
            )

    if retrieved_parts:
        joined = "\n\n".join(retrieved_parts)
        return f"<retrieved_evidence>\n{joined}\n</retrieved_evidence>", True

    no_evidence_notice = render_prompt_template("internal_revisor", "no_evidence_notice")
    return (
        f"<retrieved_evidence>\n{no_evidence_notice}\n</retrieved_evidence>",
        False,
    )


def _build_verification_user_content(
    initial_prompt: str,
    evidence_block: str,
    has_verified_evidence: bool,
    speaker: str,
    cleaned_message: str,
    cited_prior_statements: list[tuple[str, str]] | None = None,
) -> str:
    """Builds XML-isolated verification prompt for a single candidate message."""
    clean_initial = _strip_verifier_boundary_tags(initial_prompt or "")
    clean_candidate = _strip_verifier_boundary_tags(cleaned_message or "")
    clean_speaker = _strip_verifier_boundary_tags(speaker or "Agent")

    if has_verified_evidence:
        scope_mode = "evidence_backed"
        scope_parts = [render_prompt_template("internal_revisor", "scope_evidence_backed")]
    else:
        scope_mode = "open_ended"
        scope_parts = [render_prompt_template("internal_revisor", "scope_open_ended")]

    if cited_prior_statements:
        scope_parts.append(
            render_prompt_template("internal_revisor", "scope_attribution_clause")
        )

    scope_block = (
        f'<verification_scope mode="{scope_mode}">\n'
        + " ".join(scope_parts)
        + "\n</verification_scope>"
    )

    cited_block = ""
    if cited_prior_statements:
        stmt_blocks = [
            f'<agent_statement speaker="{_strip_verifier_boundary_tags(prior_sp)}">\n'
            f"{_strip_verifier_boundary_tags(prior_text)}\n"
            "</agent_statement>"
            for prior_sp, prior_text in cited_prior_statements
        ]
        cited_block = (
            '\n\n<cited_prior_statements role="non_authoritative_attribution_reference">\n'
            + "\n".join(stmt_blocks)
            + "\n</cited_prior_statements>"
        )

    return (
        "<authoritative_ground_truth>\n"
        f"<initial_user_prompt>\n{clean_initial}\n</initial_user_prompt>\n"
        f"{evidence_block}\n"
        "</authoritative_ground_truth>\n\n"
        f"{scope_block}"
        f"{cited_block}\n\n"
        f'<target_message_to_verify speaker="{clean_speaker}">\n'
        f"{clean_candidate}\n"
        "</target_message_to_verify>"
    )


def _resolve_confirmation_model() -> str:
    """Resolves the stronger Stage 2 confirmation model in a provider-agnostic manner."""
    env_confirm = os.getenv("INTERNAL_REVISOR_CONFIRM_MODEL", "").strip()
    if env_confirm:
        return env_confirm
    sys_cfg = SYSTEM_AGENTS_CONFIG.get("InternalRevisor", {})
    if isinstance(sys_cfg, dict) and sys_cfg.get("confirm_model"):
        cfg_model = str(sys_cfg["confirm_model"]).strip()
        if cfg_model:
            return cfg_model
    return get_orchestrator_reasoning_model()


def _extract_attention_warning(raw_output: str | None) -> str | None:
    """Extracts a valid ATTENTION warning or returns None for NO_HALLUCINATION/empty output."""
    if not raw_output:
        return None
    text = str(raw_output).strip()
    if not text or text.upper().startswith("NO_HALLUCINATION"):
        return None
    if "ATTENTION:" in text:
        return text
    return None


def _record_revisor_usage(ctx: TurnContext, model: str, res: Any) -> None:
    """Records token usage for an InternalRevisor LLM call."""
    if not (res and hasattr(res, "usage") and res.usage):
        return
    prompt_tokens = int(getattr(res.usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(res.usage, "completion_tokens", 0) or 0)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    try:
        from agents.usage_agent import record_thread_tokens, trigger_usage_update

        record_thread_tokens(
            channel_id=ctx.input.channel_id,
            thread_ts=ctx.input.thread_ts,
            model_name=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        trigger_usage_update(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts)
    except Exception as ex:  # noqa: BLE001
        logger.debug(f"[REVISOR] Could not record token usage for InternalRevisor: {ex}")


async def _confirm_hallucination(
    ctx: TurnContext,
    sys_prompt: str,
    ground_truth_and_target: str,
    speaker: str,
    stage1_warning: str,
    fallback_footers: list[str] | None = None,
    has_verified_evidence: bool = True,
    has_cited_prior_statements: bool = False,
) -> str | None:
    """Stage 2 confirmation using a stronger reasoning model to filter out Stage 1 false positives."""
    from core.model_catalog import format_model_fallback_footer, resolve_model_with_fallback

    confirm_res_meta = resolve_model_with_fallback(_resolve_confirmation_model(), log_context="REVISOR")
    confirm_model = confirm_res_meta.model
    if confirm_res_meta.fallback_used and fallback_footers is not None:
        fallback_footers.append(
            format_model_fallback_footer(confirm_res_meta.configured_model, confirm_model)
        )
    logger.info(
        f"[REVISOR] Stage 1 flagged potential hallucination for [{speaker}]. "
        f"Running Stage 2 confirmation with '{confirm_model}'."
    )

    if has_verified_evidence:
        citation_clause = (
            render_prompt_template(
                "internal_revisor", "stage2_evidence_backed_citation_clause"
            )
            if has_cited_prior_statements
            else ""
        )
        confirm_body = render_prompt_template(
            "internal_revisor",
            "stage2_evidence_backed",
            citation_clause=citation_clause,
        )
    else:
        citation_clause = (
            render_prompt_template(
                "internal_revisor", "stage2_open_ended_citation_clause"
            )
            if has_cited_prior_statements
            else ""
        )
        confirm_body = render_prompt_template(
            "internal_revisor",
            "stage2_open_ended",
            citation_clause=citation_clause,
        )

    confirm_user_content = (
        f"{ground_truth_and_target}\n\n"
        f"<stage1_preliminary_flag>\n{_strip_verifier_boundary_tags(stage1_warning)}\n</stage1_preliminary_flag>\n\n"
        "<confirmation_instructions>\n"
        f"{confirm_body}"
        "</confirmation_instructions>"
    )

    confirm_kwargs = {
        "model": confirm_model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": confirm_user_content},
        ],
    }

    if ctx.is_superseded():
        logger.info(
            f"[REVISOR] Stage 2 confirmation aborted for [{speaker}] — turn superseded "
            f"(ctx.my_generation={ctx.my_generation})."
        )
        return None

    confirm_res = await execute_llm_with_retry(
        func=litellm.acompletion,
        kwargs=confirm_kwargs,
        agent_key="InternalRevisorConfirm",
        thread_key=ctx.thread_key,
        allow_503_immediate_retry=False,
    )
    _record_revisor_usage(ctx, confirm_model, confirm_res)

    if confirm_res and hasattr(confirm_res, "choices") and confirm_res.choices:
        confirmed_raw = confirm_res.choices[0].message.content
        confirmed = _extract_attention_warning(confirmed_raw)
        if confirmed is None:
            logger.info(
                f"[REVISOR] Stage 2 ({confirm_model}) overturned Stage 1 flag for [{speaker}] as false positive."
            )
        return confirmed

    logger.warning(
        f"[REVISOR] Stage 2 ({confirm_model}) returned empty response for [{speaker}]; suppressing unconfirmed Stage 1 flag."
    )
    return None


async def _verify_single_candidate(
    ctx: TurnContext,
    sys_prompt: str,
    stage1_model: str,
    initial_prompt: str,
    evidence_block: str,
    has_verified_evidence: bool,
    speaker: str,
    cleaned_message: str,
    fallback_footers: list[str] | None = None,
    cited_prior_statements: list[tuple[str, str]] | None = None,
) -> str | None:
    """Verifies a single candidate message independently (Stage 1 + Stage 2 confirmation on flag)."""
    if not cleaned_message:
        return None

    from core.model_catalog import format_model_fallback_footer, resolve_model_with_fallback

    stage1_res_meta = resolve_model_with_fallback(stage1_model, log_context="REVISOR")
    active_stage1_model = stage1_res_meta.model
    if stage1_res_meta.fallback_used and fallback_footers is not None:
        fallback_footers.append(
            format_model_fallback_footer(stage1_res_meta.configured_model, active_stage1_model)
        )

    user_content = _build_verification_user_content(
        initial_prompt=initial_prompt,
        evidence_block=evidence_block,
        has_verified_evidence=has_verified_evidence,
        speaker=speaker,
        cleaned_message=cleaned_message,
        cited_prior_statements=cited_prior_statements,
    )

    kwargs = {
        "model": active_stage1_model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    if ctx.is_superseded():
        logger.info(
            f"[REVISOR] Stage 1 verification aborted for [{speaker}] — turn superseded "
            f"(ctx.my_generation={ctx.my_generation})."
        )
        return None

    res = await execute_llm_with_retry(
        func=litellm.acompletion,
        kwargs=kwargs,
        agent_key="InternalRevisor",
        thread_key=ctx.thread_key,
        allow_503_immediate_retry=False,
    )
    _record_revisor_usage(ctx, active_stage1_model, res)

    if not (res and hasattr(res, "choices") and res.choices):
        return None

    stage1_warning = _extract_attention_warning(res.choices[0].message.content)
    if not stage1_warning:
        return None

    return await _confirm_hallucination(
        ctx=ctx,
        sys_prompt=sys_prompt,
        ground_truth_and_target=user_content,
        speaker=speaker,
        stage1_warning=stage1_warning,
        fallback_footers=fallback_footers,
        has_verified_evidence=has_verified_evidence,
        has_cited_prior_statements=bool(cited_prior_statements),
    )


async def run_internal_revisor(ctx: TurnContext):
    """
    Background task to run the InternalRevisor.
    Verifies recent agent turn(s) independently against the initial user prompt and verified evidence,
    with two-stage reasoning-model confirmation before posting warnings.
    """
    try:
        # 1. Count assistant messages to determine if we are on a 2nd turn or terminating turn
        assistant_msgs = [m for m in ctx.full_history if m.get("role") == "assistant"]
        turn_count = len(assistant_msgs) + 1  # include the current turn

        rag_used = any(
            s in getattr(ctx.output, "skills_used", [])
            for s in ["knowledge_base_rag", "document_reader"]
        )
        is_terminating = (
            getattr(ctx, "force_termination", False)
            or getattr(ctx.output, "is_completed", False)
            or not thread_state_manager.get_remaining_agents(
                ctx.input.channel_id, ctx.input.thread_ts
            )
        )

        if turn_count % 2 != 0 and not (is_terminating and (turn_count > 1 or rag_used)):
            return None  # Only run on multiples of 2 or terminating turns with context

        if _is_pure_missing_skill_notice(ctx):
            logger.info(
                f"[REVISOR] Skipping InternalRevisor on turn {turn_count} (compressed missing-skill notice with no tool execution)."
            )
            return None

        if ctx.is_superseded():
            current_gen = thread_state_manager.get_generation(ctx.input.channel_id, ctx.input.thread_ts)
            logger.info(
                f"[REVISOR] Skipping InternalRevisor on turn {turn_count} — turn superseded "
                f"(ctx.my_generation={ctx.my_generation}, current_generation={current_gen})."
            )
            return None

        logger.info(f"[REVISOR] Triggering InternalRevisor on turn {turn_count}")

        # 2. Get the active round user prompt (falling back to initial_prompt / current raw_text)
        initial_prompt = (
            thread_state_manager.get_active_prompt(ctx.input.channel_id, ctx.input.thread_ts)
            or thread_state_manager.get_initial_prompt(ctx.input.channel_id, ctx.input.thread_ts)
        )
        if not initial_prompt:
            initial_prompt = ctx.input.raw_text

        # 3. Collect and clean isolated candidate turns to verify (plus citation-gated peer context)
        candidates: list[tuple[str, str, list[tuple[str, str]]]] = []
        if turn_count % 2 == 0 and assistant_msgs:
            raw_prev = str(assistant_msgs[-1].get("content", ""))
            prev_speaker = "Previous Agent"
            match = re.match(r"^\[([^\]]+)\]:", raw_prev.strip())
            if match:
                prev_speaker = match.group(1).strip()
            prev_cleaned = _clean_candidate_message(raw_prev)
            if prev_cleaned:
                prior_for_prev = _extract_prior_speaker_statements(assistant_msgs[:-1])
                prev_cited = _select_cited_prior_statements(
                    prev_cleaned, prev_speaker, prior_for_prev
                )
                candidates.append((prev_speaker, prev_cleaned, prev_cited))

        curr_speaker = ctx.agent_cfg.get("username", ctx.agent_key)
        curr_cleaned = _clean_candidate_message(ctx.output.final_text)
        if curr_cleaned:
            prior_for_curr = _extract_prior_speaker_statements(assistant_msgs)
            curr_cited = _select_cited_prior_statements(
                curr_cleaned, curr_speaker, prior_for_curr
            )
            candidates.append((curr_speaker, curr_cleaned, curr_cited))

        if not candidates:
            logger.info("[REVISOR] No non-empty candidate messages to verify.")
            return None

        # 4. Collect bounded & wrapped ground-truth evidence
        evidence_block, has_verified_evidence = _collect_ground_truth_evidence(ctx)
        sys_prompt = build_agent_prompt("InternalRevisor")
        stage1_model = get_system_agent_model(
            "InternalRevisor", fallback=get_orchestrator_fast_model()
        )

        # 5. Execute isolated per-candidate verification (concurrently if multiple candidates)
        fallback_footers: list[str] = []
        tasks = [
            _verify_single_candidate(
                ctx=ctx,
                sys_prompt=sys_prompt,
                stage1_model=stage1_model,
                initial_prompt=initial_prompt,
                evidence_block=evidence_block,
                has_verified_evidence=has_verified_evidence,
                speaker=speaker,
                cleaned_message=cleaned_msg,
                fallback_footers=fallback_footers,
                cited_prior_statements=cited_prior,
            )
            for speaker, cleaned_msg, cited_prior in candidates
        ]
        if len(tasks) == 1:
            results = [await tasks[0]]
        else:
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            for r in raw_results:
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, BaseException):
                    logger.warning(f"[REVISOR] Candidate verification raised exception: {r}")
                else:
                    results.append(r)

        confirmed_warnings = [w for w in results if w]
        if confirmed_warnings:
            combined_warning = "\n\n".join(confirmed_warnings)
            slack_text = combined_warning
            unique_footers = list(dict.fromkeys(fallback_footers))
            if unique_footers:
                slack_text = f"{combined_warning}\n\n" + "\n".join(unique_footers)
            logger.warning("[REVISOR] Hallucination confirmed! Posting to Slack.")
            sys_cfg = SYSTEM_AGENTS_CONFIG.get("InternalRevisor", {})
            display_name = sys_cfg.get("displayname") or "Internal Revisor"
            icon_url = sys_cfg.get("icon_url") or "static/image_99.png"
            await post_agent_response(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                text=slack_text,
                username=display_name,
                icon_url=icon_url,
            )
            return combined_warning

        if ctx.is_superseded():
            logger.info("[REVISOR] Verification aborted mid-flight — turn superseded.")
            return None

        logger.info("[REVISOR] No hallucinations detected.")
        return None

    except Exception as e:
        logger.exception(f"[REVISOR] Error executing InternalRevisor: {e}")
        return None
