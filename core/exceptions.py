"""
Core domain exception hierarchy for the Izvor Multi-Agent Platform.
Pure domain exceptions free of framework dependencies.
"""


class IzvorError(Exception):
    """Base exception for all Izvor platform errors."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ToolAuthorizationError(IzvorError):
    """Raised when an agent attempts to execute an unauthorized tool or skill."""


class SpendLimitExceededError(IzvorError):
    """Raised when discussion or thread spend limit exceeds configured budget thresholds."""


class CorruptedSettingsError(IzvorError):
    """Raised when settings files cannot be parsed or validated against schema."""


class OrchestrationPipelineError(IzvorError):
    """Raised when a pipeline stage fails or discussion orchestration cannot progress."""


class SkillExecutionError(IzvorError):
    """Raised when a skill or tool encounters an unhandled execution error."""


class TurnExecutionTimeoutError(IzvorError):
    """Raised when an individual agent turn or stage exceeds execution timeout."""


class ConfigurationError(IzvorError):
    """Raised when system or environment configuration is invalid."""


__all__ = [
    "IzvorError",
    "ToolAuthorizationError",
    "SpendLimitExceededError",
    "CorruptedSettingsError",
    "OrchestrationPipelineError",
    "SkillExecutionError",
    "TurnExecutionTimeoutError",
    "ConfigurationError",
]
