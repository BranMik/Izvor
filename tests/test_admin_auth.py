import base64
import pytest
from starlette.testclient import TestClient

from main import app
import core.config as config


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check_unauthenticated(client):
    """GET / and GET /health should remain public without authentication."""
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "healthy"

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "healthy"


def test_api_endpoints_reject_unauthenticated(client):
    """Admin /api/* endpoints must reject requests missing Authorization header."""
    r_skills = client.get("/api/skills")
    assert r_skills.status_code == 401
    assert "WWW-Authenticate" in r_skills.headers
    assert r_skills.headers["WWW-Authenticate"] == "Basic"

    r_status = client.get("/api/rag/status")
    assert r_status.status_code == 401
    assert r_status.headers["WWW-Authenticate"] == "Basic"

    r_reconcile = client.post("/api/rag/reconcile")
    assert r_reconcile.status_code == 401
    assert r_reconcile.headers["WWW-Authenticate"] == "Basic"


def test_api_endpoints_reject_invalid_credentials(client):
    """Admin /api/* endpoints must return 401 on wrong username or password."""
    bad_creds = base64.b64encode(b"wrong_user:wrong_pass").decode("ascii")
    headers = {"Authorization": f"Basic {bad_creds}"}

    r = client.get("/api/skills", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect username or password"


def test_api_endpoints_accept_valid_credentials(client, monkeypatch):
    """Admin /api/* endpoints must succeed when valid Basic Auth credentials are provided."""
    test_user = "testadmin"
    test_pass = "secret123"

    # Patch config and module globals in main
    monkeypatch.setattr(config, "ADMIN_USERNAME", test_user)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", test_pass)
    import main
    monkeypatch.setattr(main, "ADMIN_USERNAME", test_user)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", test_pass)

    good_creds = base64.b64encode(f"{test_user}:{test_pass}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {good_creds}"}

    r_skills = client.get("/api/skills", headers=headers)
    assert r_skills.status_code == 200
    assert "skills" in r_skills.json()

    r_status = client.get("/api/rag/status", headers=headers)
    assert r_status.status_code == 200
    assert r_status.json()["status"] == "ready"
