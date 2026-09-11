from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrchestrationMode(str, Enum):
    """Supported orchestration modes for panel discussions."""

    SANDWICH = "sandwich"
    FIXED = "fixed"


@dataclass
class FileContext:
    """Represents file attachment content and authorization metadata for a thread turn."""

    file_name: str
    content: str
    size: int = 0
    truncated: bool = False
    authorized_agents: set[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "content": self.content,
            "size": self.size,
            "truncated": self.truncated,
            "authorized_agents": self.authorized_agents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileContext":
        return cls(
            file_name=data.get("file_name", "unknown"),
            content=data.get("content", ""),
            size=data.get("size", 0),
            truncated=data.get("truncated", False),
            authorized_agents=data.get("authorized_agents"),
        )


@dataclass
class ExtractedIntent:
    """Represents structured user directives extracted by LLM or slash commands."""

    max_words: int | None = None
    history: int | None = None
    file_use: bool = False
    target_file: str | None = None
    excluded_agents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_words": self.max_words,
            "history": self.history,
            "file_use": self.file_use,
            "target_file": self.target_file,
            "excluded_agents": self.excluded_agents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedIntent":
        return cls(
            max_words=data.get("max_words"),
            history=data.get("history"),
            file_use=bool(data.get("file_use", False)),
            target_file=data.get("target_file"),
            excluded_agents=list(data.get("excluded_agents") or []),
        )


@dataclass
class CircuitBreakerConfig:
    """Configuration for monetary expense limits per thread."""

    enabled: bool = False
    threshold_usd: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "threshold_usd": self.threshold_usd,
        }


@dataclass
class HistoryLimitConfig:
    """Configuration for thread history truncation window."""

    limit: int
    prior_cutoff_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "prior_cutoff_index": self.prior_cutoff_index,
        }


@dataclass
class ModelPricing:
    """Represents pricing per million tokens for an LLM model."""

    input_price_per_1m: float
    output_price_per_1m: float
    currency: str = "USD"
    source: str = "Unknown"
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "currency": self.currency,
            "source": self.source,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelPricing":
        return cls(
            input_price_per_1m=float(data.get("input_price_per_1m", 0.0)),
            output_price_per_1m=float(data.get("output_price_per_1m", 0.0)),
            currency=data.get("currency", "USD"),
            source=data.get("source", "Unknown"),
            source_url=data.get("source_url", ""),
        )


@dataclass
class WorkflowPreset:
    """Represents a named orchestration workflow preset with custom agent chains."""

    id: str
    name: str
    mode: str = OrchestrationMode.SANDWICH.value
    description: str = ""
    sandwich_config: dict[str, list[str]] = field(
        default_factory=lambda: {"start_chain": [], "end_chain": []}
    )
    fixed_config: dict[str, list[str]] = field(
        default_factory=lambda: {"chain": []}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "description": self.description,
            "sandwich_config": self.sandwich_config,
            "fixed_config": self.fixed_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowPreset":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            mode=str(data.get("mode", OrchestrationMode.SANDWICH.value)),
            description=str(data.get("description", "")),
            sandwich_config=dict(
                data.get("sandwich_config") or {"start_chain": [], "end_chain": []}
            ),
            fixed_config=dict(data.get("fixed_config") or {"chain": []}),
        )

