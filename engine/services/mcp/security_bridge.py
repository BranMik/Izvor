"""
Security bridge and gateway for Model Context Protocol (MCP) tool execution.

Implements Roadmap R-04 defenses:
1. Fail-closed ToolPrincipal authorization check before dispatch.
2. Outbound argument sanitization (preventing secret exfiltration via MCP parameters).
3. Cryptographic audit logging with arguments SHA-256 hash.
4. Inbound response secret scanning and PII masking.
"""

import copy
import hashlib
import json
import logging
from typing import Any

from core.domain import ToolPrincipal
from orchestration.system_settings import get_mcp_server_config
from services.mcp.client_manager import McpClientManager, get_mcp_client_manager
from services.mcp.schema_translator import parse_mcp_tool_name

logger = logging.getLogger(__name__)


def sanitize_outbound_mcp_arguments(
    arguments: dict[str, Any],
    mask_enabled: bool = True,
) -> dict[str, Any]:
    """
    Recursively scans and masks PII/secrets from arguments before passing them
    to an external MCP server process or network endpoint.
    """
    if not mask_enabled or not arguments:
        return copy.deepcopy(arguments) if arguments else {}

    from services.security.pii_masker import pii_masker

    def _sanitize(val: Any) -> Any:
        if isinstance(val, str):
            masked, _ = pii_masker.mask(val)
            return masked
        elif isinstance(val, dict):
            return {k: _sanitize(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_sanitize(item) for item in val]
        return val

    return {k: _sanitize(v) for k, v in arguments.items()}


def sanitize_inbound_mcp_result(raw_result: str) -> str:
    """
    Scans raw output from an MCP server for leaked secrets and applies PII masking
    before the content is injected into LLM conversation turns or returned to caller.
    """
    if not raw_result:
        return raw_result

    from services.security import scan_output_for_secrets
    from services.security.pii_masker import pii_masker

    # 1. Secret leakage detection (fail-closed if sensitive keys/tokens detected)
    matched_rule = scan_output_for_secrets(raw_result, user_id="mcp_server")
    if matched_rule:
        logger.critical(
            f"[MCP_SECURITY] Blocked inbound MCP tool response containing leaked secret matching rule: {matched_rule}"
        )
        return json.dumps({
            "isError": True,
            "error": f"Output blocked by security policy: secret detected ({matched_rule})",
        })

    # 2. PII masking
    masked, _ = pii_masker.mask(raw_result)
    return masked


async def execute_external_mcp_tool(
    namespaced_tool_name: str,
    arguments: dict[str, Any] | None = None,
    agent_name: ToolPrincipal | str | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
    client_manager: McpClientManager | None = None,
) -> str:
    """
    Executes an external MCP tool via McpClientManager, strictly enforcing
    ToolPrincipal authorization, argument sanitization, audit logging, and response safety.
    """
    parsed = parse_mcp_tool_name(namespaced_tool_name)
    if not parsed:
        return json.dumps({
            "error": f"Invalid MCP tool name '{namespaced_tool_name}'; expected 'mcp__<server>__<tool>' format.",
        })

    from services.audit_logger import log_audit_event

    server_name, raw_tool_name = parsed
    raw_args = arguments or {}
    args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    actor_id = agent_name.id if hasattr(agent_name, "id") else str(agent_name or "unknown")
    actor_kind = getattr(agent_name, "kind", "agent")

    logger.info(
        f"[MCP Tool Call] Executing '{namespaced_tool_name}' ({server_name}.{raw_tool_name}) "
        f"keys={list(raw_args.keys())} args_sha256={args_hash} (agent: {actor_id})"
    )

    # 1. Defense-in-depth authorization check
    from skills.loader import is_principal_authorized_for_tool

    if not is_principal_authorized_for_tool(
        principal=agent_name,
        tool_name=namespaced_tool_name,
        authorized_skills=authorized_skills,
        skill_registry=skill_registry,
    ):
        logger.warning(
            f"[MCP_SECURITY] Access denied: Principal '{actor_id}' is not authorized to execute MCP tool '{namespaced_tool_name}'."
        )
        log_audit_event(
            event_type="SECURITY_POLICY_VIOLATION",
            actor={"type": actor_kind, "id": actor_id},
            action="execute_external_mcp_tool",
            target=f"mcp:{namespaced_tool_name}",
            details={"reason": "Defense-in-depth authorization check failed", "args_sha256": args_hash},
            severity="CRITICAL",
            status="BLOCKED",
        )
        return json.dumps({
            "error": f"Permission denied: Agent '{actor_id}' is not authorized to execute tool '{namespaced_tool_name}'."
        })

    # 2. Server configuration check
    server_cfg = get_mcp_server_config(server_name)
    if not server_cfg:
        logger.error(f"[MCP_SECURITY] MCP server '{server_name}' is not configured.")
        return json.dumps({"error": f"MCP server '{server_name}' is not configured."})
    if not server_cfg.enabled:
        logger.error(f"[MCP_SECURITY] MCP server '{server_name}' is disabled.")
        return json.dumps({"error": f"MCP server '{server_name}' is disabled."})

    # 3. Outbound PII / Secret masking
    sanitized_args = sanitize_outbound_mcp_arguments(
        raw_args,
        mask_enabled=server_cfg.outbound_pii_masking,
    )

    # 4. Non-repudiable audit logging
    log_audit_event(
        event_type="TOOL_EXECUTE_MCP",
        actor={"type": actor_kind, "id": actor_id},
        action=namespaced_tool_name,
        target=f"mcp:{server_name}/{raw_tool_name}",
        details={
            "server": server_name,
            "tool": raw_tool_name,
            "args_keys": list(raw_args.keys()),
            "args_sha256": args_hash,
        },
        severity="INFO",
        status="SUCCESS",
    )

    # 5. Dispatch invocation to McpClientManager
    mgr = client_manager or get_mcp_client_manager()
    raw_res = await mgr.call_mcp_tool(server_name, raw_tool_name, sanitized_args)

    # 6. Inbound response sanitization
    return sanitize_inbound_mcp_result(raw_res)
