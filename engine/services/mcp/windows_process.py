"""
Windows subprocess confinement and environment sanitization for MCP servers.

Ensures that child MCP server processes:
1. Cannot inherit Izvor's internal secrets (Slack tokens, HMAC secret, admin passwords).
2. Are assigned to a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
   guaranteeing they are automatically terminated by the Windows kernel if the parent process dies.
"""

from collections.abc import Mapping
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Sensitive environment variables that must never leak to external MCP processes
BLOCKED_ENV_VARS: set[str] = {
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_APP_TOKEN",
    "AUDIT_HMAC_SECRET",
    "ADMIN_PASSWORD",
    "INTERNAL_API_KEY",
}

# Standard system environment variables preserved for subprocesses to function
SAFE_INHERITED_VARS: set[str] = {
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROCESSOR_ARCHITECTURE",
    "USERNAME",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "NODE_PATH",
    "NVM_HOME",
    "NVM_SYMLINK",
}

# Module-level storage for active Windows Job Objects to keep handles alive
_ACTIVE_JOB_HANDLES: list[Any] = []


def build_safe_stdio_env(
    extra_env: dict[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Builds a sanitized environment dictionary for an MCP stdio subprocess.

    Preserves safe system variables (e.g. PATH, SYSTEMROOT, TEMP), sets UTF-8
    encoding variables, merges explicitly allowed extra_env, and unconditionally
    strips Izvor internal secrets (case-insensitively).
    """
    clean_env: dict[str, str] = {}
    active_base = base_env if base_env is not None else os.environ
    safe_upper = {v.upper() for v in SAFE_INHERITED_VARS}
    blocked_upper = {v.upper() for v in BLOCKED_ENV_VARS}

    # 1. Inherit safe system variables from current environment (case-insensitive)
    for k, v in active_base.items():
        key_upper = str(k).upper()
        if key_upper in safe_upper and key_upper not in blocked_upper:
            clean_env[str(k)] = str(v)

    # 2. Set default UTF-8 encoding for Python / CLI children
    clean_env["PYTHONIOENCODING"] = "utf-8"
    clean_env["PYTHONUTF8"] = "1"

    # 3. Merge explicitly configured extra_env (e.g. API keys for the specific MCP server)
    if extra_env:
        for k, v in extra_env.items():
            key_str = str(k).strip()
            if key_str:
                clean_env[key_str] = str(v)

    # 4. Strip forbidden sensitive variables unconditionally (case-insensitive)
    for k in list(clean_env.keys()):
        if k.upper() in blocked_upper:
            del clean_env[k]

    return clean_env


def assign_process_to_windows_job_object(pid: int) -> Any:
    """
    Assigns a running process (by PID) to a Windows Job Object configured to
    terminate all member processes when the parent job handle is closed
    (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000).

    Returns the job handle on success, or None on non-Windows platforms or failure.
    """
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        # Constants from winnt.h
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", wintypes.ULARGE_INTEGER),
                ("WriteOperationCount", wintypes.ULARGE_INTEGER),
                ("OtherOperationCount", wintypes.ULARGE_INTEGER),
                ("ReadTransferCount", wintypes.ULARGE_INTEGER),
                ("WriteTransferCount", wintypes.ULARGE_INTEGER),
                ("OtherTransferCount", wintypes.ULARGE_INTEGER),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Create Job Object
        hJob = kernel32.CreateJobObjectW(None, None)
        if not hJob:
            logger.warning("[MCP_PROCESS] Failed to create Windows Job Object")
            return None

        # Configure KILL_ON_JOB_CLOSE
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        res = kernel32.SetInformationJobObject(
            hJob,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            logger.warning("[MCP_PROCESS] Failed to set Job Object info with KILL_ON_JOB_CLOSE")
            kernel32.CloseHandle(hJob)
            return None

        # Open process and assign to job
        hProcess = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hProcess:
            logger.warning(f"[MCP_PROCESS] Failed to OpenProcess for PID {pid}")
            kernel32.CloseHandle(hJob)
            return None

        try:
            assign_res = kernel32.AssignProcessToJobObject(hJob, hProcess)
            if not assign_res:
                logger.warning(f"[MCP_PROCESS] Failed to AssignProcessToJobObject for PID {pid}")
                kernel32.CloseHandle(hJob)
                return None
        finally:
            kernel32.CloseHandle(hProcess)

        _ACTIVE_JOB_HANDLES.append(hJob)
        logger.debug(f"[MCP_PROCESS] Successfully assigned PID {pid} to Windows Job Object")
        return hJob

    except Exception as e:  # noqa: BLE001
        logger.warning(f"[MCP_PROCESS] Error configuring Windows Job Object for PID {pid}: {e}")
        return None


def close_windows_job_handle(h_job: Any) -> bool:
    """Closes a Windows Job Object handle (terminating child processes when KILL_ON_JOB_CLOSE is set)."""
    if sys.platform != "win32" or not h_job:
        return False
    try:
        if h_job in _ACTIVE_JOB_HANDLES:
            _ACTIVE_JOB_HANDLES.remove(h_job)
    except ValueError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return bool(kernel32.CloseHandle(h_job))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[MCP_PROCESS] Error closing Windows Job Object handle: {e}")
        return False


def close_all_windows_job_handles() -> int:
    """Closes all tracked Windows Job Object handles and returns the count closed."""
    closed = 0
    for handle in list(_ACTIVE_JOB_HANDLES):
        if close_windows_job_handle(handle):
            closed += 1
    _ACTIVE_JOB_HANDLES.clear()
    return closed

