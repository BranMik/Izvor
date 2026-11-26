"""
Component Readiness Registry for Izvor (Tier L2).
Tracks startup lifecycle and per-component health without upward dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Literal

ComponentState = Literal["pending", "ready", "degraded", "failed"]


@dataclass
class ComponentStatus:
    """Tracks readiness state for a single application subsystem."""

    status: ComponentState
    detail: str = ""
    critical: bool = False
    updated_at: float = 0.0


class ReadinessRegistry:
    """Thread-safe registry tracking startup completion and subsystem readiness."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._startup_complete: bool = False
        self._components: dict[str, ComponentStatus] = {}

    def mark_component(
        self,
        name: str,
        status: ComponentState,
        detail: str = "",
        critical: bool = False,
    ) -> None:
        """Records or updates readiness status for a named subsystem."""
        clean_name = str(name).strip()
        if not clean_name:
            return
        with self._lock:
            self._components[clean_name] = ComponentStatus(
                status=status,
                detail=str(detail or "").strip(),
                critical=bool(critical),
                updated_at=time.time(),
            )

    def mark_startup_complete(self, complete: bool = True) -> None:
        """Marks whether initial lifespan startup warmups have finished."""
        with self._lock:
            self._startup_complete = bool(complete)

    def reset(self) -> None:
        """Resets all readiness state (primarily for test isolation)."""
        with self._lock:
            self._startup_complete = False
            self._components.clear()

    def get_summary(self) -> dict[str, Any]:
        """
        Computes overall readiness summary.
        HTTP 503 gating is strictly limited to incomplete startup or failed/degraded critical components.
        Non-critical warmup failures surface as status='degraded' with ready=True (HTTP 200).
        """
        with self._lock:
            startup_complete = self._startup_complete
            components_snapshot = {
                name: {
                    "status": comp.status,
                    "detail": comp.detail,
                    "critical": comp.critical,
                    "updated_at": comp.updated_at,
                }
                for name, comp in sorted(self._components.items())
            }

        has_critical_failure = any(
            c["critical"] and c["status"] in ("failed", "degraded")
            for c in components_snapshot.values()
        )
        has_non_critical_degradation = any(
            not c["critical"] and c["status"] in ("failed", "degraded")
            for c in components_snapshot.values()
        )

        if has_critical_failure:
            overall_status = "unhealthy"
            is_ready = False
        elif not startup_complete:
            overall_status = "starting"
            is_ready = False
        elif has_non_critical_degradation:
            overall_status = "degraded"
            is_ready = True
        else:
            overall_status = "ready"
            is_ready = True

        return {
            "status": overall_status,
            "ready": is_ready,
            "startup_complete": startup_complete,
            "components": components_snapshot,
        }


readiness_registry = ReadinessRegistry()


def reset_readiness_state() -> None:
    """Resets global readiness_registry state for clean test isolation."""
    readiness_registry.reset()

