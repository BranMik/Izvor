from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config_store.system_settings import system_settings_manager
from core.config import get_admin_password, get_admin_username, is_production
from core.slack_security import extract_client_ip
from services.rag.sync_engine import rag_sync_engine

logger = logging.getLogger("IzvorAPI")

# HTTP Basic Authentication security schemes for admin and docs endpoints
admin_security = HTTPBasic()
docs_security = HTTPBasic(auto_error=False)


def verify_admin_basic_auth(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(admin_security),
) -> str:
    """
    Verifies HTTP Basic Auth credentials against ADMIN_USERNAME and ADMIN_PASSWORD.
    Enforces rate-limit lockout BEFORE credential verification so brute-forced IPs
    cannot succeed even with valid credentials during an active lockout window.
    Uses secrets.compare_digest to defend against timing attacks.
    """
    from services.audit_logger import log_audit_event
    from services.rate_limiter import ip_rate_limiter

    fallback_ip = request.client.host if request.client and request.client.host else "unknown_ip"
    client_ip = extract_client_ip(request.headers, fallback_host=fallback_ip)

    burst_limits = system_settings_manager.get_security_burst_limits()
    max_failures = int(burst_limits.get("auth_failure_burst_limit", 5))
    window_secs = float(burst_limits.get("auth_failure_window_seconds", 60.0))

    admin_pass = get_admin_password()
    admin_user = get_admin_username()

    if not admin_pass:
        logger.error("ADMIN_PASSWORD is not configured in environment/.env")
        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "ADMIN_PASSWORD not configured on server", "client_ip": client_ip},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin authentication is not configured on the server",
        )

    is_blocked, attempts = ip_rate_limiter.is_blocked(
        client_ip=client_ip,
        max_requests=max_failures,
        window_seconds=window_secs,
    )
    if is_blocked:
        if ip_rate_limiter.mark_block_logged(client_ip):
            log_audit_event(
                event_type="AUTH_RATE_LIMITED",
                actor={"type": "api_client", "id": credentials.username},
                action="admin_basic_auth",
                target="/api/*",
                details={
                    "reason": "Excessive failed basic auth attempts (throttled)",
                    "attempts": attempts,
                    "client_ip": client_ip,
                },
                severity="CRITICAL",
                status="BLOCKED",
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts. Please wait before retrying.",
        )

    is_correct_username = secrets.compare_digest(credentials.username.encode("utf8"), admin_user.encode("utf8"))
    is_correct_password = secrets.compare_digest(credentials.password.encode("utf8"), admin_pass.encode("utf8"))

    if not (is_correct_username and is_correct_password):
        is_allowed, attempts, should_log = ip_rate_limiter.check_and_record(
            client_ip=client_ip,
            max_requests=max_failures,
            window_seconds=window_secs,
        )

        if not is_allowed:
            if should_log:
                log_audit_event(
                    event_type="AUTH_RATE_LIMITED",
                    actor={"type": "api_client", "id": credentials.username},
                    action="admin_basic_auth",
                    target="/api/*",
                    details={
                        "reason": "Excessive failed basic auth attempts (throttled)",
                        "attempts": attempts,
                        "client_ip": client_ip,
                    },
                    severity="CRITICAL",
                    status="BLOCKED",
                )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed authentication attempts. Please wait before retrying.",
            )

        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "Invalid credentials provided", "client_ip": client_ip},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    ip_rate_limiter.reset(client_ip)
    return credentials.username


def verify_docs_auth_in_production(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(docs_security),
) -> None:
    """Enforces Admin Basic Auth on OpenAPI/Swagger routes in production; open in dev/test."""
    if not is_production():
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )
    verify_admin_basic_auth(request, credentials)


def get_rag_sync_engine() -> Any:
    """FastAPI dependency provider returning the active RAG reconciliation engine."""
    return rag_sync_engine
