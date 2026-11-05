from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class OrchestrationMode(StrEnum):
    """Supported orchestration modes for panel discussions."""

    SANDWICH = "sandwich"
    FIXED = "fixed"
    SCATTER_GATHER = "scatter_gather"

    @classmethod
    def from_value(cls, val: str | "OrchestrationMode") -> "OrchestrationMode":
        if isinstance(val, cls):
            return val
        val_str = str(val or "").strip().lower()
        try:
            return cls(val_str)
        except ValueError:
            return cls.SANDWICH


REQUIRED_DEFAULT_WORKFLOW_IDS: tuple[str, ...] = (
    "default_review_flow",
    "default_sandwich",
    "parallel_dialectic_flow",
)


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
            "authorized_agents": sorted(self.authorized_agents) if self.authorized_agents is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileContext":
        raw_auth = data.get("authorized_agents")
        auth_set = set(raw_auth) if raw_auth is not None else None
        return cls(
            file_name=data.get("file_name", "unknown"),
            content=data.get("content", ""),
            size=data.get("size", 0),
            truncated=data.get("truncated", False),
            authorized_agents=auth_set,
        )


class AttachmentType(str, Enum):
    """Supported attachment media categories."""

    TEXT = "text"
    CODE = "code"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"


@dataclass
class MediaAttachment:
    """Represents a media or document attachment for multimodal context."""

    file_id: str
    name: str
    mimetype: str
    attachment_type: AttachmentType = AttachmentType.TEXT
    size: int = 0
    raw_bytes: bytes | None = None
    b64_data: str | None = None
    authorized_agents: set[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mimetype": self.mimetype,
            "attachment_type": self.attachment_type.value
            if isinstance(self.attachment_type, AttachmentType)
            else str(self.attachment_type),
            "size": self.size,
            "b64_data": self.b64_data,
            "authorized_agents": list(self.authorized_agents) if self.authorized_agents is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaAttachment":
        raw_type = data.get("attachment_type", AttachmentType.TEXT)
        try:
            att_type = AttachmentType(raw_type)
        except ValueError:
            att_type = AttachmentType.TEXT

        raw_auth = data.get("authorized_agents")
        auth_set = set(raw_auth) if raw_auth is not None else None

        return cls(
            file_id=data.get("file_id", ""),
            name=data.get("name", "unknown"),
            mimetype=data.get("mimetype", "text/plain"),
            attachment_type=att_type,
            size=data.get("size", 0),
            raw_bytes=data.get("raw_bytes"),
            b64_data=data.get("b64_data"),
            authorized_agents=auth_set,
        )


@dataclass
class ActionDirective:
    """Represents an executable pipeline or orchestrator action with scoped parameters."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    origin: str = "slash"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionDirective":
        return cls(
            name=data.get("name", ""),
            params=dict(data.get("params") or {}),
            origin=data.get("origin", "slash"),
        )


@dataclass
class ExtractedIntent:
    """Represents structured user directives and actions extracted by slash commands or LLM."""

    actions: list[ActionDirective] = field(default_factory=list)
    modifiers: dict[str, Any] = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)
    detected_language: str | None = None

    def has_action(self, name: str) -> bool:
        """Check if an action is present in actions list."""
        return any(a.name == name for a in self.actions)

    def get_actions(self, name: str) -> list[ActionDirective]:
        """Get all ActionDirectives matching name."""
        return [a for a in self.actions if a.name == name]

    def get_modifier(self, name: str, default: Any = None) -> Any:
        """Retrieve a modifier value by name."""
        return self.modifiers.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "modifiers": dict(self.modifiers),
            "attachments": [att.to_dict() for att in self.attachments],
            "detected_language": self.detected_language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedIntent":
        raw_actions = data.get("actions") or []
        actions = [ActionDirective.from_dict(a) if isinstance(a, dict) else a for a in raw_actions]

        modifiers = dict(data.get("modifiers") or {})

        raw_attachments = data.get("attachments") or []
        attachments = [MediaAttachment.from_dict(att) if isinstance(att, dict) else att for att in raw_attachments]

        return cls(
            actions=actions,
            modifiers=modifiers,
            attachments=attachments,
            detected_language=data.get("detected_language"),
        )


@dataclass
class CircuitBreakerConfig:
    """Configuration for monetary expense limits per thread."""

    enabled: bool = False
    threshold_usd: float = 1.0
    daily_budget_usd: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "threshold_usd": self.threshold_usd,
            "daily_budget_usd": self.daily_budget_usd,
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
class WorkflowPreset:
    """Represents a named orchestration workflow preset with custom agent chains."""

    id: str
    name: str
    mode: str = OrchestrationMode.SANDWICH.value
    description: str = ""
    sandwich_config: dict[str, list[str]] = field(default_factory=lambda: {"start_chain": [], "end_chain": []})
    fixed_config: dict[str, list[str]] = field(default_factory=lambda: {"chain": []})

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "description": self.description,
        }
        mode_normalized = str(self.mode or "").strip().lower()
        if mode_normalized == OrchestrationMode.FIXED.value:
            data["fixed_config"] = self.fixed_config
        else:
            data["sandwich_config"] = self.sandwich_config
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowPreset":
        raw_mode = str(data.get("mode", OrchestrationMode.SANDWICH.value)).strip().lower()
        valid_modes = (
            OrchestrationMode.SANDWICH.value,
            OrchestrationMode.FIXED.value,
            OrchestrationMode.SCATTER_GATHER.value,
            "scatter_gather",
        )
        mode = raw_mode if raw_mode in valid_modes else OrchestrationMode.SANDWICH.value
        if mode == OrchestrationMode.FIXED.value:
            fixed_config = dict(data.get("fixed_config") or {"chain": []})
            sandwich_config = {"start_chain": [], "end_chain": []}
        else:
            sandwich_config = dict(data.get("sandwich_config") or {"start_chain": [], "end_chain": []})
            fixed_config = {"chain": []}

        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            mode=mode,
            description=str(data.get("description", "")),
            sandwich_config=sandwich_config,
            fixed_config=fixed_config,
        )


def build_dynamic_default_workflows(agent_ids: list[str]) -> list[WorkflowPreset]:
    """
    Dynamically constructs canonical default workflow presets from available agent IDs.
    Guarantees that default presets are never statically bound to hardcoded agent IDs.
    """
    agents = list(agent_ids)
    if not agents:
        fixed_chain: list[str] = []
        sw_start: list[str] = []
        sw_end: list[str] = []
        pd_start: list[str] = []
        pd_end: list[str] = []
    else:
        fixed_chain = agents[:5] if len(agents) >= 5 else list(agents)
        sw_start = agents[: min(5, len(agents))]
        sw_end = list(reversed(agents[-min(3, len(agents)) :])) if len(agents) > 1 else list(agents)
        pd_start = [agents[0]]
        pd_end = [agents[-1]] if len(agents) > 1 else [agents[0]]

    return [
        WorkflowPreset(
            id="default_review_flow",
            name="Default Review Workflow",
            mode=OrchestrationMode.FIXED.value,
            description="[DO NOT DELETE OR MODIFY - SYSTEM DEFAULT] Standard review flow applied when no explicit agent is tagged.",
            fixed_config={"chain": fixed_chain},
            sandwich_config={},
        ),
        WorkflowPreset(
            id="default_sandwich",
            name="Default Sandwich Review",
            mode=OrchestrationMode.SANDWICH.value,
            description="[DO NOT DELETE OR MODIFY - SYSTEM DEFAULT] Standard multi-agent panel with kickoff and concluding wrap-up.",
            sandwich_config={
                "start_chain": sw_start,
                "end_chain": sw_end,
            },
            fixed_config={"chain": []},
        ),
        WorkflowPreset(
            id="parallel_dialectic_flow",
            name="Parallel Dialectic (Scatter-Gather)",
            mode=OrchestrationMode.SCATTER_GATHER.value,
            description="Concurrent parallel exploration where specialist agents investigate the anchor problem simultaneously without sequential anchoring bias, followed by dialectical synthesis.",
            sandwich_config={
                "start_chain": pd_start,
                "end_chain": pd_end,
            },
            fixed_config={"chain": []},
        ),
    ]
