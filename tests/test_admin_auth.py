import base64
import pytest
from starlette.testclient import TestClient

from main import app
import core.config as config


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check_unauthenticated(client):
    """GET / and GET /health should remain public without authentication and without leaking agent lists."""
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "healthy"
    assert r_root.json()["service"] == "Izvor Multi-Agent Hub"
    assert "agents" not in r_root.json()

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "healthy"
    assert r_health.json()["service"] == "Izvor Multi-Agent Hub"
    assert "agents" not in r_health.json()


def test_api_endpoints_reject_unauthenticated(client):
    """Admin /api/* endpoints must reject requests missing Authorization header."""
    r_health = client.get("/api/health")
    assert r_health.status_code == 401
    assert "WWW-Authenticate" in r_health.headers
    assert r_health.headers["WWW-Authenticate"] == "Basic"

    r_skills = client.get("/api/skills")
    assert r_skills.status_code == 401
    assert "WWW-Authenticate" in r_skills.headers
    assert r_skills.headers["WWW-Authenticate"] == "Basic"

    r_status = client.get("/api/rag/status")
    assert r_status.status_code == 401
    assert "WWW-Authenticate" in r_status.headers
    assert r_status.headers["WWW-Authenticate"] == "Basic"

    r_reconcile = client.post("/api/rag/reconcile")
    assert r_reconcile.status_code == 401
    assert r_reconcile.headers["WWW-Authenticate"] == "Basic"

    r_audit = client.get("/api/audit/status")
    assert r_audit.status_code == 401
    assert "WWW-Authenticate" in r_audit.headers
    assert r_audit.headers["WWW-Authenticate"] == "Basic"


def test_api_endpoints_reject_invalid_credentials(client):
    """Admin /api/* endpoints must return 401 on wrong username or password."""
    bad_creds = base64.b64encode(b"wrong_user:wrong_pass").decode("ascii")
    headers = {"Authorization": f"Basic {bad_creds}"}

    r = client.get("/api/skills", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect username or password"


def test_api_endpoints_accept_valid_credentials(client):
    """Admin /api/* endpoints must succeed when valid Basic Auth credentials are provided."""
    test_user = "testadmin"
    test_pass = "secret123"

    config.set_admin_credentials_override(test_user, test_pass)
    try:
        good_creds = base64.b64encode(f"{test_user}:{test_pass}".encode()).decode("ascii")
        headers = {"Authorization": f"Basic {good_creds}"}

        r_health = client.get("/api/health", headers=headers)
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "healthy"
        assert r_health.json()["service"] == "Izvor Multi-Agent Hub"
        assert "agents" in r_health.json()
        assert isinstance(r_health.json()["agents"], list)

        r_skills = client.get("/api/skills", headers=headers)
        assert r_skills.status_code == 200
        assert "skills" in r_skills.json()

        r_status = client.get("/api/rag/status", headers=headers)
        assert r_status.status_code == 200
        assert r_status.json()["status"] == "ready"

        r_audit = client.get("/api/audit/status", headers=headers)
        assert r_audit.status_code == 200
        assert "valid" in r_audit.json()
        assert "total_records" in r_audit.json()
    finally:
        config.set_admin_credentials_override(None, None)


def test_trigger_rag_reconciliation_audit_event(client, monkeypatch):
    """POST /api/rag/reconcile must emit RAG_RECONCILE_TRIGGERED audit event."""
    from unittest.mock import AsyncMock

    test_user = "testadmin"
    test_pass = "secret123"

    config.set_admin_credentials_override(test_user, test_pass)
    try:
        import main
        monkeypatch.setattr(main.rag_sync_engine, "reconcile", AsyncMock(return_value={"reconciled": 5}))

        logged_events = []
        def mock_log_event(**kwargs):
            logged_events.append(kwargs)
            return kwargs

        monkeypatch.setattr("services.audit_logger.log_audit_event", mock_log_event)

        good_creds = base64.b64encode(f"{test_user}:{test_pass}".encode()).decode("ascii")
        headers = {"Authorization": f"Basic {good_creds}"}

        r = client.post("/api/rag/reconcile", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "reconciliation_complete"
        assert r.json()["stats"] == {"reconciled": 5}

        reconcile_events = [e for e in logged_events if e.get("event_type") == "RAG_RECONCILE_COMPLETED"]
        assert len(reconcile_events) >= 1
        assert reconcile_events[0]["actor"]["id"] == test_user
    finally:
        config.set_admin_credentials_override(None, None)

