"""Unit tests for readiness registry, critical-only 503 gating, /api/traces, and /api/metrics (TKT-1242)."""

from __future__ import annotations

import base64
from fastapi.testclient import TestClient

import main
from core.config import set_admin_credentials_override
from core.database import get_db_transaction
from services.readiness import readiness_registry
from services.telemetry import flush_turn_traces, record_turn_trace


def _basic_auth_header(username: str = "admin", password: str = "secret-pass") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def test_readiness_critical_only_503_gating() -> None:
    client = TestClient(main.app)
    set_admin_credentials_override("admin", "secret-pass")
    try:
        # 1. Before startup completes -> 503 starting
        r_start = client.get("/health/ready")
        assert r_start.status_code == 503
        assert r_start.json() == {"status": "starting", "service": "Izvor Multi-Agent Hub"}

        # 2. Startup completes + database ready + non-critical warmup degraded -> 200 degraded
        readiness_registry.mark_component("database", "ready", critical=True)
        readiness_registry.mark_component("pricing_cache", "degraded", detail="TimeoutError: remote down", critical=False)
        readiness_registry.mark_startup_complete(True)

        r_deg = client.get("/health/ready")
        assert r_deg.status_code == 200
        assert r_deg.json()["status"] == "degraded"

        r_admin_deg = client.get("/api/health/ready", headers=_basic_auth_header())
        assert r_admin_deg.status_code == 200
        body_deg = r_admin_deg.json()
        assert body_deg["status"] == "degraded"
        assert body_deg["startup_complete"] is True
        assert body_deg["components"]["pricing_cache"]["detail"] == "TimeoutError: remote down"

        # 3. Critical component failure -> 503 unhealthy
        readiness_registry.mark_component("database", "failed", detail="OperationalError: disk full", critical=True)
        r_unhealthy = client.get("/health/ready")
        assert r_unhealthy.status_code == 503
        assert r_unhealthy.json()["status"] == "unhealthy"
        assert client.get("/api/health/ready", headers=_basic_auth_header()).status_code == 503

        # 4. All healthy -> 200 ready
        readiness_registry.mark_component("database", "ready", critical=True)
        readiness_registry.mark_component("pricing_cache", "ready", critical=False)
        assert client.get("/health/ready").status_code == 200
        assert client.get("/health/ready").json()["status"] == "ready"
    finally:
        set_admin_credentials_override(None, None)


def test_observability_admin_endpoints_auth_traces_and_metrics() -> None:
    client = TestClient(main.app)
    set_admin_credentials_override("admin", "secret-pass")
    try:
        # Auth enforcement
        assert client.get("/api/health/ready").status_code == 401
        assert client.get("/api/traces").status_code == 401
        assert client.get("/api/metrics").status_code == 401

        readiness_registry.mark_component("database", "ready", critical=True)
        readiness_registry.mark_startup_complete(True)

        record_turn_trace(
            session_id="C_OBS:999.1",
            channel_id="C_OBS",
            thread_ts="999.1",
            agent_key="agent-10001",
            stage_name="execution",
            duration_ms=42.5,
            prompt_tokens=120,
            completion_tokens=45,
            tools_called=["web_search"],
            status="SUCCESS",
        )
        flush_turn_traces()

        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO spend_ledger (channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, cost_usd, timestamp)
                VALUES ('C_OBS', '999.1', 'gemini/gemini-3.8-flash', 120, 45, 0.0125, 1700000000.0);
                """
            )

        # Traces endpoint + filtering
        tr_resp = client.get(
            "/api/traces?channel_id=C_OBS&thread_ts=999.1&agent_key=agent-10001&status=SUCCESS&limit=10",
            headers=_basic_auth_header(),
        )
        assert tr_resp.status_code == 200
        tr_data = tr_resp.json()
        assert tr_data["count"] == 1
        assert tr_data["traces"][0]["stage_name"] == "execution"
        assert tr_data["traces"][0]["tools_called"] == ["web_search"]

        # Limit validation bounds (1..500)
        assert client.get("/api/traces?limit=501", headers=_basic_auth_header()).status_code == 422

        # Prometheus metrics exposition
        m_resp = client.get("/api/metrics", headers=_basic_auth_header())
        assert m_resp.status_code == 200
        assert m_resp.headers["content-type"].startswith("text/plain; version=0.0.4")
        text = m_resp.text
        assert "# TYPE izvor_up gauge" in text
        assert "izvor_up 1" in text
        assert 'izvor_turn_traces_total{stage_name="execution",status="SUCCESS"} 1' in text
        assert "izvor_spend_usd_total 0.0125" in text
        assert 'izvor_tokens_total{token_type="prompt"} 120' in text
        assert 'izvor_tokens_total{token_type="completion"} 45' in text
    finally:
        set_admin_credentials_override(None, None)

