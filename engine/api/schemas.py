from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PublicHealthResponse(BaseModel):
    """Public unauthenticated service health check payload."""

    status: str
    service: str


class AdminHealthResponse(BaseModel):
    """Authenticated admin health check payload with registered agent catalog."""

    status: str
    service: str
    agents: list[str]


class SkillItemResponse(BaseModel):
    """Metadata and tool schemas for a single registered skill."""

    name: str
    display_name: str
    description: str
    tools: list[str | None]
    schemas: list[dict[str, Any]]


class SkillsListResponse(BaseModel):
    """Response envelope listing all registered skills."""

    skills: list[SkillItemResponse]


class RagStatusResponse(BaseModel):
    """RAG manifest status and indexed documents overview."""

    status: str
    total_documents: int
    documents: dict[str, dict[str, Any]]


class RagReconcileResponse(BaseModel):
    """Result of an on-demand RAG knowledge reconciliation scan."""

    status: str
    stats: dict[str, Any]


class ThreadDeleteResponse(BaseModel):
    """Confirmation payload for permanent GDPR thread erasure."""

    status: str
    channel_id: str
    thread_ts: str
    deleted: dict[str, Any]


class PublicReadinessResponse(BaseModel):
    """Minimal unauthenticated readiness probe response."""

    status: str
    service: str


class ComponentReadinessItem(BaseModel):
    """Readiness details for a single application subsystem."""

    status: str
    detail: str
    critical: bool
    updated_at: float


class AdminReadinessResponse(BaseModel):
    """Detailed authenticated readiness probe response."""

    status: str
    service: str
    startup_complete: bool
    components: dict[str, ComponentReadinessItem]


class TurnTraceItemResponse(BaseModel):
    """Single TurnPipeline stage execution trace span."""

    trace_id: str
    session_id: str
    channel_id: str
    thread_ts: str
    agent_key: str
    stage_name: str
    duration_ms: float
    prompt_tokens: int
    completion_tokens: int
    tools_called: list[str]
    status: str
    error_message: str | None = None
    created_at: float


class TurnTracesResponse(BaseModel):
    """Response envelope listing queried turn execution traces."""

    count: int
    traces: list[TurnTraceItemResponse]
