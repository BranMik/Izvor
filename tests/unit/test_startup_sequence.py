"""
Unit tests verifying the startup sequence and lifecycle warm-up ordering.
Ensures Presidio warm-up and cache reconciliations are completed before
the '[STARTUP] Application Startup Complete' log is emitted.
"""

from __future__ import annotations

import logging
import pytest
from fastapi.testclient import TestClient

from main import app, lifespan

pytestmark = pytest.mark.presidio


@pytest.mark.anyio
async def test_lifespan_warmup_order_and_completion_log(caplog):
    """
    Verify that entering the application lifespan completes warmups
    and logs '[STARTUP] Application Startup Complete' before yielding.
    """
    caplog.set_level(logging.INFO)

    async with lifespan(app):
        # Once lifespan yields, all warmups must be complete
        from services.security.pii_masker import pii_masker

        assert pii_masker._presidio_initialized is True

        messages = [rec.getMessage() for rec in caplog.records]
        startup_complete_idx = None
        pii_warmup_idx = None

        for idx, msg in enumerate(messages):
            if "[PII Masker] Warm-up complete" in msg:
                pii_warmup_idx = idx
            if "[STARTUP] Application Startup Complete" in msg:
                startup_complete_idx = idx

        assert startup_complete_idx is not None, "Expected '[STARTUP] Application Startup Complete' in logs"
        if pii_warmup_idx is not None:
            assert pii_warmup_idx < startup_complete_idx, (
                f"Presidio warm-up log at {pii_warmup_idx} must precede startup complete log at {startup_complete_idx}"
            )


def test_testclient_startup_lifecycle():
    """
    Verify that TestClient successfully runs the full startup lifecycle without hanging or error.
    """
    with TestClient(app) as client:
        response = client.get("/docs")
        assert response.status_code == 200

