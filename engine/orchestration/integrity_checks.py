"""
Centralized application integrity checks module (L3).

Inherits settings/workflow integrity validation from ConfigIntegrityValidator (L1.5)
and adds runtime audit log chain verification (L2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config_store.integrity import ConfigIntegrityValidator

logger = logging.getLogger(__name__)


class AppIntegrityValidator(ConfigIntegrityValidator):
    """Application startup and runtime integrity checks including audit log verification."""

    @classmethod
    def check_audit_log_integrity(
        cls,
        audit_dir: Path | None = None,
        month: str | None = None,
    ) -> dict[str, Any]:
        """
        Validates the non-repudiable audit log cryptographic hash chain and HMAC key continuity.
        Returns a dictionary with status, errors, hmac_continuity, and verification details.
        """
        from services.audit_logger import (
            check_hmac_secret_continuity,
            verify_audit_log_integrity,
        )

        hmac_continuity = check_hmac_secret_continuity(audit_dir=audit_dir)
        if not hmac_continuity["matches"]:
            logger.warning(f"[INTEGRITY] AUDIT_HMAC_SECRET continuity check warning: {hmac_continuity['message']}")

        result = verify_audit_log_integrity(audit_dir=audit_dir, month=month)
        if not result.valid:
            for err in result.errors:
                logger.critical(f"[INTEGRITY] Audit log verification failed: {err}")
        else:
            logger.info(
                f"[INTEGRITY] Audit log chain verified successfully ({result.total_records} records in {len(result.scanned_files)} files)."
            )
        return {
            "valid": result.valid,
            "total_records": result.total_records,
            "scanned_files": result.scanned_files,
            "errors": result.errors,
            "last_verified_seq": result.last_verified_seq,
            "last_verified_hash": result.last_verified_hash,
            "hmac_continuity": hmac_continuity,
        }
