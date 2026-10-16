import pytest
from services.audit_logger import audit_logger


@pytest.fixture(autouse=True)
def isolate_audit_logger_for_tests(tmp_path):
    """
    Bulletproof isolation of the AuditLogger.
    Forces all audit logging during pytest runs to write to a temporary directory.
    Because this is a pytest fixture, it cannot be triggered in production.
    """
    test_audit_dir = tmp_path / "audit"
    test_audit_dir.mkdir(parents=True, exist_ok=True)

    old_dir = audit_logger.audit_dir

    audit_logger.audit_dir = test_audit_dir
    audit_logger.invalidate_cache()

    yield

    audit_logger.audit_dir = old_dir
    audit_logger.invalidate_cache()
def pytest_addoption(parser):
    parser.addoption(
        "--mock",
        "--offline",
        dest="mock",
        action="store_true",
        default=False,
        help="Runs offline in-memory simulation for E2E scenarios",
    )
    parser.addoption(
        "--hybrid--mocked-llm",
        "--hybrid-mocked-llm",
        dest="hybrid_mocked_llm",
        action="store_true",
        default=False,
        help="Activates hybrid mocked LLM testing (default is live LLM execution for all agents)",
    )
