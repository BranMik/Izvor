"""
Non-repudiable audit logging service for security-critical events in SilvanAgora.

Features:
- Cryptographic SHA-256 hash chaining (Merkle-style chain) with optional HMAC verification.
- Monthly file partitioning (audit/audit_YYYY_MM.jsonl) with cross-month chain continuity.
- Append-only thread-safe writing with thread lock.
- Archival snapshot backups (audit/backups/) and SHA-256 integrity manifest.
- Comprehensive integrity verification engine detecting content tampering, line deletions,
  and chain disruptions.
- Standalone CLI interface for verification, snapshotting, and health stats.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Default directory paths
DEFAULT_AUDIT_DIR = PROJECT_ROOT / "audit"
DEFAULT_BACKUP_DIR = DEFAULT_AUDIT_DIR / "backups"
DEFAULT_MANIFEST_FILE = DEFAULT_AUDIT_DIR / "manifest.sha256"

# Genesis hash for the initial record (seq: 1)
GENESIS_HASH = "0" * 64

# Secret HMAC key from environment (optional defense against offline recalculation)
AUDIT_HMAC_SECRET = os.getenv("AUDIT_HMAC_SECRET", "").encode("utf-8")


@dataclass
class AuditActor:
    type: str  # e.g., 'slack_admin', 'slack_user', 'agent', 'system', 'api_client'
    id: str
    ip: str | None = None


@dataclass
class AuditRecord:
    seq: int
    timestamp: str  # ISO 8601 UTC
    event_type: str
    severity: str  # 'INFO', 'WARNING', 'CRITICAL'
    actor: dict[str, Any]
    action: str
    target: str
    details: dict[str, Any]
    status: str  # 'SUCCESS', 'FAILED', 'BLOCKED'
    prev_hash: str
    entry_hash: str = ""

    def to_canonical_dict(self) -> dict[str, Any]:
        """Returns the dictionary representation excluding entry_hash for canonical hashing."""
        data = asdict(self)
        data.pop("entry_hash", None)
        return data


def canonical_serialize(data: dict[str, Any]) -> str:
    """Serializes a dictionary to canonical JSON (sorted keys, compact separators, UTF-8)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_entry_hash(prev_hash: str, canonical_json: str, secret_key: bytes | None = None) -> str:
    """Computes SHA-256 (or HMAC-SHA256 if secret_key is provided) entry hash."""
    message = (prev_hash + canonical_json).encode("utf-8")
    key = secret_key if secret_key else AUDIT_HMAC_SECRET
    if key:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


class AuditLogger:
    """
    Singleton service managing non-repudiable audit trails.
    Guarantees thread-safe append writes and sequence continuity.
    """

    _instance: AuditLogger | None = None
    _instance_lock = threading.Lock()

    def __new__(cls, audit_dir: Path | None = None) -> AuditLogger:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, audit_dir: Path | None = None) -> None:
        if getattr(self, "_initialized", False):
            if audit_dir and audit_dir != self.audit_dir:
                self.audit_dir = audit_dir
                self.backup_dir = self.audit_dir / "backups"
                self.manifest_file = self.audit_dir / "manifest.sha256"
                self._ensure_dirs()
            return

        self.audit_dir = audit_dir or DEFAULT_AUDIT_DIR
        self.backup_dir = self.audit_dir / "backups"
        self.manifest_file = self.audit_dir / "manifest.sha256"
        self._write_lock = threading.Lock()
        self._ensure_dirs()
        self._initialized = True

    def _ensure_dirs(self) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def get_month_file_path(self, dt: datetime.datetime | None = None) -> Path:
        """Returns the monthly partitioned file path: audit/audit_YYYY_MM.jsonl."""
        timestamp = dt or datetime.datetime.now(datetime.timezone.utc)
        filename = f"audit_{timestamp.strftime('%Y_%m')}.jsonl"
        return self.audit_dir / filename

    def _get_sorted_audit_files(self) -> list[Path]:
        """Returns all monthly audit files sorted chronologically."""
        if not self.audit_dir.exists():
            return []
        files = list(self.audit_dir.glob("audit_*.jsonl"))
        files.sort(key=lambda p: p.name)
        return files

    def get_last_state(self) -> tuple[int, str]:
        """
        Scans existing audit files to determine the highest sequence ID and last entry_hash.
        If no logs exist, returns (0, GENESIS_HASH).
        """
        sorted_files = self._get_sorted_audit_files()
        if not sorted_files:
            return 0, GENESIS_HASH

        # Inspect newest file from the end
        for file_path in reversed(sorted_files):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                    if lines:
                        last_line = lines[-1]
                        record = json.loads(last_line)
                        return int(record["seq"]), str(record["entry_hash"])
            except Exception as e:
                logger.error(f"[AUDIT] Failed to read last state from {file_path.name}: {e}")
                continue

        return 0, GENESIS_HASH

    def log_event(
        self,
        event_type: str,
        actor: AuditActor | dict[str, Any],
        action: str,
        target: str,
        details: dict[str, Any] | None = None,
        severity: str = "INFO",
        status: str = "SUCCESS",
        timestamp: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """
        Records an audit event with cryptographic hash chaining.
        Thread-safe and append-only.
        """
        now = timestamp or datetime.datetime.now(datetime.timezone.utc)
        iso_timestamp = now.isoformat()
        target_file = self.get_month_file_path(now)

        actor_dict = asdict(actor) if isinstance(actor, AuditActor) else dict(actor)
        clean_details = details or {}

        with self._write_lock:
            last_seq, last_hash = self.get_last_state()
            current_seq = last_seq + 1
            prev_hash = last_hash

            record = AuditRecord(
                seq=current_seq,
                timestamp=iso_timestamp,
                event_type=event_type,
                severity=severity,
                actor=actor_dict,
                action=action,
                target=target,
                details=clean_details,
                status=status,
                prev_hash=prev_hash,
            )

            canonical = canonical_serialize(record.to_canonical_dict())
            record.entry_hash = compute_entry_hash(prev_hash, canonical)

            # Append atomic line to monthly audit file
            line_str = json.dumps(asdict(record), ensure_ascii=False) + "\n"
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(line_str)
                f.flush()

            logger.info(
                f"[AUDIT] Seq {record.seq} ({record.event_type}) recorded by {actor_dict.get('id')} -> {target_file.name}"
            )
            return asdict(record)

    def create_snapshot(self) -> Path | None:
        """
        Creates an archival read-only snapshot of current month audit log in audit/backups/
        and updates audit/manifest.sha256.
        """
        current_file = self.get_month_file_path()
        if not current_file.exists():
            return None

        timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_name = f"{current_file.stem}_{timestamp_str}.jsonl.snapshot"
        snapshot_path = self.backup_dir / snapshot_name

        with self._write_lock:
            content = current_file.read_bytes()
            snapshot_path.write_bytes(content)
            checksum = hashlib.sha256(content).hexdigest()

            # Append to manifest
            manifest_line = f"{checksum}  {snapshot_name}\n"
            with open(self.manifest_file, "a", encoding="utf-8") as mf:
                mf.write(manifest_line)

            logger.info(f"[AUDIT] Created backup snapshot: {snapshot_name} (SHA-256: {checksum[:12]}...)")
            return snapshot_path


# Global module singleton instance
audit_logger = AuditLogger()


def log_audit_event(
    event_type: str,
    actor: AuditActor | dict[str, Any],
    action: str,
    target: str,
    details: dict[str, Any] | None = None,
    severity: str = "INFO",
    status: str = "SUCCESS",
) -> dict[str, Any]:
    """Helper entrypoint for emitting non-repudiable audit events."""
    return audit_logger.log_event(
        event_type=event_type,
        actor=actor,
        action=action,
        target=target,
        details=details,
        severity=severity,
        status=status,
    )


# ---------------------------------------------------------------------------
# Verification Engine
# ---------------------------------------------------------------------------


@dataclass
class AuditVerificationResult:
    valid: bool
    total_records: int
    scanned_files: list[str]
    errors: list[str]
    last_verified_seq: int
    last_verified_hash: str


def verify_audit_log_integrity(
    audit_dir: Path | None = None,
    month: str | None = None,
) -> AuditVerificationResult:
    """
    Verifies the cryptographic hash chain across audit files.

    Checks:
    1. Sequence continuity (seq monotonically increasing by 1).
    2. Chain link continuity (current.prev_hash == previous.entry_hash).
    3. Content integrity (current.entry_hash == recomputed_hash).

    Args:
        audit_dir: Optional override for audit directory.
        month: Optional specific month to check (e.g., '2026_10' or '2026-10').
    """
    base_dir = audit_dir or DEFAULT_AUDIT_DIR
    if not base_dir.exists():
        return AuditVerificationResult(
            valid=True,
            total_records=0,
            scanned_files=[],
            errors=[],
            last_verified_seq=0,
            last_verified_hash=GENESIS_HASH,
        )

    if month:
        clean_month = month.replace("-", "_")
        target_file = base_dir / f"audit_{clean_month}.jsonl"
        files = [target_file] if target_file.exists() else []
    else:
        files = sorted(list(base_dir.glob("audit_*.jsonl")), key=lambda p: p.name)

    if not files:
        return AuditVerificationResult(
            valid=True,
            total_records=0,
            scanned_files=[],
            errors=[],
            last_verified_seq=0,
            last_verified_hash=GENESIS_HASH,
        )

    expected_prev_hash = GENESIS_HASH
    expected_seq = 1
    total_records = 0
    scanned_file_names: list[str] = []
    errors: list[str] = []

    # If checking a single month that is not the first, determine starting prev_hash from previous file
    if month and files:
        all_files = sorted(list(base_dir.glob("audit_*.jsonl")), key=lambda p: p.name)
        target_idx = all_files.index(files[0]) if files[0] in all_files else -1
        if target_idx > 0:
            prev_file = all_files[target_idx - 1]
            try:
                with open(prev_file, "r", encoding="utf-8") as pf:
                    p_lines = [line.strip() for line in pf if line.strip()]
                    if p_lines:
                        last_p = json.loads(p_lines[-1])
                        expected_seq = int(last_p["seq"]) + 1
                        expected_prev_hash = str(last_p["entry_hash"])
            except Exception as e:
                errors.append(f"Could not read previous boundary file '{prev_file.name}': {e}")

    for file_path in files:
        scanned_file_names.append(file_path.name)
        line_num = 0
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_num += 1
                    clean_line = line.strip()
                    if not clean_line:
                        continue

                    total_records += 1
                    try:
                        record_dict = json.loads(clean_line)
                    except json.JSONDecodeError as err:
                        errors.append(
                            f"File '{file_path.name}', line {line_num}: Malformed JSON - {err}"
                        )
                        return AuditVerificationResult(
                            valid=False,
                            total_records=total_records,
                            scanned_files=scanned_file_names,
                            errors=errors,
                            last_verified_seq=expected_seq - 1,
                            last_verified_hash=expected_prev_hash,
                        )

                    seq = record_dict.get("seq")
                    prev_hash = record_dict.get("prev_hash")
                    recorded_entry_hash = record_dict.get("entry_hash", "")

                    # 1. Sequence check
                    if seq != expected_seq:
                        errors.append(
                            f"File '{file_path.name}', line {line_num}: Sequence mismatch. "
                            f"Expected seq {expected_seq}, but found {seq}."
                        )

                    # 2. Previous hash link check
                    if prev_hash != expected_prev_hash:
                        errors.append(
                            f"File '{file_path.name}', line {line_num}: Chain broken. "
                            f"Expected prev_hash '{expected_prev_hash}', but record has '{prev_hash}'."
                        )

                    # 3. Content integrity check
                    record_obj = AuditRecord(
                        seq=record_dict["seq"],
                        timestamp=record_dict["timestamp"],
                        event_type=record_dict["event_type"],
                        severity=record_dict.get("severity", "INFO"),
                        actor=record_dict["actor"],
                        action=record_dict["action"],
                        target=record_dict["target"],
                        details=record_dict.get("details", {}),
                        status=record_dict.get("status", "SUCCESS"),
                        prev_hash=prev_hash,
                    )
                    canonical = canonical_serialize(record_obj.to_canonical_dict())
                    computed_hash = compute_entry_hash(prev_hash, canonical)

                    if computed_hash != recorded_entry_hash:
                        errors.append(
                            f"File '{file_path.name}', line {line_num}: Content tampered. "
                            f"Recorded entry_hash '{recorded_entry_hash}' does not match "
                            f"computed hash '{computed_hash}'."
                        )

                    expected_seq = (seq if isinstance(seq, int) else expected_seq) + 1
                    expected_prev_hash = recorded_entry_hash

        except Exception as e:
            errors.append(f"File '{file_path.name}', line {line_num}: Read error - {e}")

    is_valid = len(errors) == 0
    return AuditVerificationResult(
        valid=is_valid,
        total_records=total_records,
        scanned_files=scanned_file_names,
        errors=errors,
        last_verified_seq=expected_seq - 1,
        last_verified_hash=expected_prev_hash,
    )


async def send_admin_audit_alert(
    title: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """
    Sends an urgent security alert in English directly to the administrator's Slack DM
    using ADMIN_SLACK_USER_ID, with fallback to SLACK_TEST_CHANNEL_ID.
    """
    from core.config import ADMIN_SLACK_USER_ID, SLACK_BOT_TOKEN, SLACK_TEST_CHANNEL_ID
    from services.slack_client import open_dm_channel, post_agent_response

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.warning(f"[MOCK SLACK ALERT] {title}\n{message}\nDetails: {details}")
        return True

    target_channel = None
    if ADMIN_SLACK_USER_ID:
        try:
            target_channel = await open_dm_channel(ADMIN_SLACK_USER_ID)
        except Exception as e:
            logger.error(f"[AUDIT] Failed to open DM with admin {ADMIN_SLACK_USER_ID}: {e}")

    if not target_channel:
        target_channel = SLACK_TEST_CHANNEL_ID

    if not target_channel:
        logger.warning("[AUDIT] No admin Slack target configured for security alert.")
        return False

    text_parts = [
        f"🚨 *[SECURITY CRITICAL] {title}*",
        message,
    ]
    if details:
        for k, v in details.items():
            text_parts.append(f"• *{k}:* `{v}`")
    text_parts.append(
        "👉 _Action Required: Run `python -m services.audit_logger --verify` for forensic diagnostics._"
    )
    alert_text = "\n".join(text_parts)

    try:
        await post_agent_response(
            channel_id=target_channel,
            thread_ts="",
            text=alert_text,
            username="Security Audit Guard",
        )
        logger.info(f"[AUDIT] Dispatched security alert DM to {target_channel}")
        return True
    except Exception as e:
        logger.error(f"[AUDIT] Error dispatching security alert to Slack: {e}")
        return False


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def cli_main() -> None:
    parser = argparse.ArgumentParser(
        description="SilvanAgora Non-Repudiable Audit Log CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the cryptographic integrity of audit logs across files.",
    )
    parser.add_argument(
        "--month",
        type=str,
        help="Filter verification or stats to a specific month (e.g. 2026-10 or 2026_10).",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Create a backup snapshot and update the SHA-256 manifest.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Display statistics and overview of audit logs.",
    )

    args = parser.parse_args()

    if args.snapshot:
        logger.info("Triggering manual audit snapshot...")
        snap = audit_logger.create_snapshot()
        if snap:
            print(f"[OK] Archival snapshot created: {snap.name}")
        else:
            print("[INFO] No current audit log found to snapshot.")
        return

    if args.stats:
        files = audit_logger._get_sorted_audit_files()
        total_bytes = sum(f.stat().st_size for f in files)
        last_seq, last_hash = audit_logger.get_last_state()
        print("\n=== SilvanAgora Audit Log Statistics ===")
        print(f"Audit Directory:    {audit_logger.audit_dir}")
        print(f"Total Log Files:    {len(files)}")
        print(f"Total Size:         {total_bytes / 1024:.2f} KB ({total_bytes} bytes)")
        print(f"Total Events (Seq): {last_seq}")
        print(f"Latest Entry Hash:  {last_hash[:16]}...{last_hash[-8:]}")
        print("Files:")
        for f in files:
            print(f"  - {f.name} ({f.stat().st_size} bytes)")
        print("=========================================\n")
        return

    # Default action or --verify
    result = verify_audit_log_integrity(month=args.month)
    print("\n=== SilvanAgora Audit Log Integrity Verification ===")
    print(f"Scanned Files:    {', '.join(result.scanned_files) or 'None'}")
    print(f"Total Records:    {result.total_records}")
    print(f"Last Seq Number:  {result.last_verified_seq}")
    print(f"Last Entry Hash:  {result.last_verified_hash}")

    if result.valid:
        print("\n[VERIFIED] All cryptographic audit chains are fully intact. No tampering detected.\n")
    else:
        print("\n[FAILED] INTEGRITY BREACH DETECTED!")
        print("Identified Violations:")
        for err in result.errors:
            print(f"  ❌ {err}")
        print("\nImmediate action required: Check backup snapshots or server security logs.\n")
        exit(1)


if __name__ == "__main__":
    cli_main()
