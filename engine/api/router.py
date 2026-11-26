from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from api.dependencies import (
    get_rag_sync_engine,
    verify_admin_basic_auth,
    verify_docs_auth_in_production,
)
from api.schemas import (
    AdminHealthResponse,
    AdminReadinessResponse,
    PublicHealthResponse,
    PublicReadinessResponse,
    RagReconcileResponse,
    RagStatusResponse,
    SkillsListResponse,
    ThreadDeleteResponse,
    TurnTracesResponse,
)
from core.config import AGENTS_CONFIG

public_router = APIRouter(tags=["Public & Docs"])
api_router = APIRouter(tags=["Admin API"])


@public_router.get("/", response_model=PublicHealthResponse)
@public_router.get("/health", response_model=PublicHealthResponse)
async def health_check() -> PublicHealthResponse:
    """Basic public health check endpoint."""
    return PublicHealthResponse(
        status="healthy",
        service="Izvor Multi-Agent Hub",
    )


@public_router.get("/health/ready", response_model=PublicReadinessResponse)
async def public_readiness_check() -> JSONResponse:
    """
    Minimal unauthenticated readiness probe for container/load-balancer health checks.
    Returns HTTP 200 when critically ready ('ready' or non-fatal 'degraded'), and HTTP 503
    when startup is incomplete ('starting') or a critical subsystem failed ('unhealthy').
    """
    from services.readiness import readiness_registry

    summary = readiness_registry.get_summary()
    code = status.HTTP_200_OK if summary["ready"] else status.HTTP_503_SERVICE_UNAVAILABLE
    payload = PublicReadinessResponse(
        status=summary["status"],
        service="Izvor Multi-Agent Hub",
    )
    return JSONResponse(content=payload.model_dump(), status_code=code)


@public_router.get("/openapi.json", include_in_schema=False)
async def get_openapi_schema(
    request: Request,
    _: None = Depends(verify_docs_auth_in_production),
) -> JSONResponse:
    """Returns OpenAPI schema JSON (Basic-Auth gated in production)."""
    return JSONResponse(request.app.openapi())


@public_router.get("/docs", include_in_schema=False)
async def get_swagger_ui(
    request: Request,
    _: None = Depends(verify_docs_auth_in_production),
) -> HTMLResponse:
    """Serves Swagger UI (Basic-Auth gated in production)."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{request.app.title} - Swagger UI")


@public_router.get("/redoc", include_in_schema=False)
async def get_redoc_ui(
    request: Request,
    _: None = Depends(verify_docs_auth_in_production),
) -> HTMLResponse:
    """Serves ReDoc UI (Basic-Auth gated in production)."""
    return get_redoc_html(openapi_url="/openapi.json", title=f"{request.app.title} - ReDoc")


@api_router.get(
    "/health",
    dependencies=[Depends(verify_admin_basic_auth)],
    response_model=AdminHealthResponse,
)
async def admin_health_check() -> AdminHealthResponse:
    """Admin health check endpoint exposing registered agent catalog."""
    return AdminHealthResponse(
        status="healthy",
        service="Izvor Multi-Agent Hub",
        agents=list(AGENTS_CONFIG.keys()),
    )


@api_router.get(
    "/skills",
    dependencies=[Depends(verify_admin_basic_auth)],
    response_model=SkillsListResponse,
)
async def get_registered_skills() -> SkillsListResponse:
    """Returns all registered skills, descriptions, and their tool schemas."""
    from core.registry import get_skill_registry

    skill_registry = get_skill_registry()
    skills_data = []
    for s in skill_registry.list_skills():
        skills_data.append(
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "tools": [t.get("function", {}).get("name") for t in s.get_tool_schemas()],
                "schemas": s.get_tool_schemas(),
            }
        )
    return SkillsListResponse(skills=skills_data)


@api_router.get(
    "/rag/status",
    dependencies=[Depends(verify_admin_basic_auth)],
    response_model=RagStatusResponse,
)
async def get_rag_status() -> RagStatusResponse:
    """Returns RAG manifest status and indexed documents overview."""

    def _read_manifest() -> dict[str, Any]:
        from services.rag.manifest_manager import ManifestManager

        manifest = ManifestManager()
        records = manifest.get_all_records()
        return {
            "status": "ready",
            "total_documents": len(records),
            "documents": records,
        }

    data = await asyncio.to_thread(_read_manifest)
    return RagStatusResponse(**data)


@api_router.post("/rag/reconcile", response_model=RagReconcileResponse)
async def trigger_rag_reconciliation(
    admin_user: str = Depends(verify_admin_basic_auth),
    sync_engine: Any = Depends(get_rag_sync_engine),
) -> RagReconcileResponse:
    """Triggers an on-demand reconciliation scan of rag_knowledge/."""
    from services.audit_logger import log_audit_event

    try:
        stats = await sync_engine.reconcile()
        errors = stats.get("errors") or []
        audit_status = "PARTIAL_FAILURE" if errors else "SUCCESS"
        audit_severity = "WARNING" if errors else "INFO"
        resp_status = "reconciliation_partial_failure" if errors else "reconciliation_complete"
        log_audit_event(
            event_type="RAG_RECONCILE_COMPLETED",
            actor={"type": "admin_user", "id": admin_user},
            action="reconcile_rag_knowledge",
            target="rag_knowledge/",
            details={"source": "api_endpoint", "stats": stats},
            severity=audit_severity,
            status=audit_status,
        )
        return RagReconcileResponse(status=resp_status, stats=stats)
    except Exception as e:
        log_audit_event(
            event_type="RAG_RECONCILE_FAILED",
            actor={"type": "admin_user", "id": admin_user},
            action="reconcile_rag_knowledge",
            target="rag_knowledge/",
            details={"source": "api_endpoint", "error": str(e)},
            severity="CRITICAL",
            status="FAILED",
        )
        raise


@api_router.get("/audit/status", dependencies=[Depends(verify_admin_basic_auth)])
async def get_audit_status() -> dict[str, Any]:
    """Returns non-repudiable audit log cryptographic integrity status and statistics."""
    from orchestration.integrity_checks import AppIntegrityValidator

    return await asyncio.to_thread(AppIntegrityValidator.check_audit_log_integrity)


@api_router.delete("/threads/{channel_id}/{thread_ts}", response_model=ThreadDeleteResponse)
async def delete_thread_data(
    channel_id: str,
    thread_ts: str,
    admin_user: str = Depends(verify_admin_basic_auth),
) -> ThreadDeleteResponse:
    """Permanently purges persisted state, flat histories, notes, downloads, and caches for a thread."""
    from services.audit_logger import log_audit_event_async
    from services.retention_service import forget_thread

    deleted = await asyncio.to_thread(forget_thread, channel_id, thread_ts)
    await log_audit_event_async(
        event_type="GDPR_THREAD_FORGOTTEN",
        actor={"type": "admin_user", "id": admin_user},
        action="forget_thread",
        target=f"{channel_id}:{thread_ts}",
        details=deleted,
        severity="WARNING",
        status="SUCCESS",
    )
    return ThreadDeleteResponse(
        status="forgotten",
        channel_id=channel_id,
        thread_ts=thread_ts,
        deleted=deleted,
    )


@api_router.get(
    "/health/ready",
    dependencies=[Depends(verify_admin_basic_auth)],
    response_model=AdminReadinessResponse,
)
async def admin_readiness_check() -> JSONResponse:
    """
    Detailed authenticated readiness probe reporting startup completion and per-component health.
    Returns HTTP 200 for 'ready' or non-fatal 'degraded', and HTTP 503 for 'starting' or 'unhealthy'.
    """
    from services.readiness import readiness_registry

    summary = readiness_registry.get_summary()
    code = status.HTTP_200_OK if summary["ready"] else status.HTTP_503_SERVICE_UNAVAILABLE
    payload = AdminReadinessResponse(
        status=summary["status"],
        service="Izvor Multi-Agent Hub",
        startup_complete=summary["startup_complete"],
        components=summary["components"],
    )
    return JSONResponse(content=payload.model_dump(), status_code=code)


@api_router.get(
    "/traces",
    dependencies=[Depends(verify_admin_basic_auth)],
    response_model=TurnTracesResponse,
)
async def list_turn_traces(
    session_id: str | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    agent_key: str | None = None,
    trace_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> TurnTracesResponse:
    """Queries recorded TurnPipeline stage execution spans after flushing buffered telemetry."""
    from services.telemetry import get_turn_traces

    rows = await asyncio.to_thread(
        get_turn_traces,
        session_id=session_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key=agent_key,
        status=trace_status,
        limit=limit,
    )
    return TurnTracesResponse(count=len(rows), traces=rows)


@api_router.get(
    "/metrics",
    dependencies=[Depends(verify_admin_basic_auth)],
)
async def get_prometheus_metrics() -> PlainTextResponse:
    """Exposes runtime, readiness, telemetry, and spend metrics in Prometheus v0.0.4 text format."""
    from services.metrics import render_prometheus_metrics

    body = await asyncio.to_thread(render_prometheus_metrics)
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
