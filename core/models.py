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
            "attachment_type": self.attachment_type.value if isinstance(self.attachment_type, AttachmentType) else str(self.attachment_type),
            "size": self.size,
            "b64_data": self.b64_data,
            "authorized_agents": list(self.authorized_agents) if self.authorized_agents else None,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionDirective":
        return cls(
            name=data.get("name", ""),
            params=dict(data.get("params") or {}),
        )


@dataclass
class ExtractedIntent:
    """Represents structured user directives and actions extracted by slash commands or LLM."""

    actions: list[ActionDirective] = field(default_factory=list)
    modifiers: dict[str, Any] = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)

    def __init__(
        self,
        actions: list[ActionDirective] | None = None,
        modifiers: dict[str, Any] | None = None,
        attachments: list[MediaAttachment] | None = None,
        max_words: int | None = None,
        min_words: int | None = None,
        history: int | None = None,
        file_use: bool = False,
        target_file: str | None = None,
        excluded_agents: list[str] | None = None,
        **extra_modifiers: Any,
    ):
        self.actions = list(actions or [])
        self.modifiers = dict(modifiers or {})
        self.attachments = list(attachments or [])

        if max_words is not None:
            self.max_words = max_words
        if min_words is not None:
            self.min_words = min_words
        if history is not None:
            self.history = history
        if file_use:
            self.file_use = True
        if target_file is not None:
            self.target_file = target_file
        if excluded_agents:
            self.excluded_agents = list(excluded_agents)

        for k, v in extra_modifiers.items():
            if v is not None:
                self.modifiers[k] = v

    # Clean accessors & backward compatibility
    @property
    def max_words(self) -> int | None:
        return self.modifiers.get("max_words")

    @max_words.setter
    def max_words(self, val: int | None) -> None:
        if val is not None:
            self.modifiers["max_words"] = val
        else:
            self.modifiers.pop("max_words", None)

    @property
    def min_words(self) -> int | None:
        return self.modifiers.get("min_words")

    @min_words.setter
    def min_words(self, val: int | None) -> None:
        if val is not None:
            self.modifiers["min_words"] = val
        else:
            self.modifiers.pop("min_words", None)

    @property
    def history(self) -> int | None:
        return self.modifiers.get("history")

    @history.setter
    def history(self, val: int | None) -> None:
        if val is not None:
            self.modifiers["history"] = val
        else:
            self.modifiers.pop("history", None)

    @property
    def file_use(self) -> bool:
        if self.modifiers.get("file_use", False):
            return True
        try:
            from skills import skill_registry
            file_skill = skill_registry.get_file_handler_skill()
            if file_skill and self.has_action(file_skill.name):
                return True
        except (ImportError, AttributeError):
            file_skill = None
        return any("target_file" in a.params for a in self.actions)

    @file_use.setter
    def file_use(self, val: bool) -> None:
        self.modifiers["file_use"] = bool(val)
        try:
            from skills import skill_registry
            file_skill = skill_registry.get_file_handler_skill()
            skill_name = file_skill.name if file_skill else None
        except (ImportError, AttributeError):
            skill_name = None

        if skill_name:
            if val and not self.has_action(skill_name):
                self.actions.append(ActionDirective(name=skill_name))
            elif not val and self.has_action(skill_name):
                self.actions = [a for a in self.actions if a.name != skill_name]

    @property
    def target_file(self) -> str | None:
        for a in self.actions:
            if a.params.get("target_file"):
                return str(a.params["target_file"])
        return self.modifiers.get("target_file")

    @target_file.setter
    def target_file(self, val: str | None) -> None:
        if val is not None:
            self.modifiers["target_file"] = val
            try:
                from skills import skill_registry
                file_skill = skill_registry.get_file_handler_skill()
                skill_name = file_skill.name if file_skill else None
            except (ImportError, AttributeError):
                skill_name = None

            matched = [a for a in self.actions if (skill_name and a.name == skill_name) or "target_file" in a.params]
            if matched:
                matched[0].params["target_file"] = val
            elif skill_name:
                self.actions.append(ActionDirective(name=skill_name, params={"target_file": val}))
        else:
            self.modifiers.pop("target_file", None)
            for a in self.actions:
                a.params.pop("target_file", None)

    @property
    def excluded_agents(self) -> list[str]:
        exc_actions = self.get_actions("exclude_agents")
        if exc_actions:
            return list(exc_actions[0].params.get("agents", []))
        return list(self.modifiers.get("excluded_agents", []))

    @excluded_agents.setter
    def excluded_agents(self, val: list[str]) -> None:
        agents = list(val or [])
        self.modifiers["excluded_agents"] = agents
        exc_actions = self.get_actions("exclude_agents")
        if exc_actions:
            exc_actions[0].params["agents"] = agents
        elif agents:
            self.actions.append(ActionDirective(name="exclude_agents", params={"agents": agents}))

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
            # Legacy fields mirrored for backward serialization compatibility
            "max_words": self.max_words,
            "min_words": self.min_words,
            "history": self.history,
            "file_use": self.file_use,
            "target_file": self.target_file,
            "excluded_agents": self.excluded_agents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedIntent":
        raw_actions = data.get("actions") or []
        actions = [
            ActionDirective.from_dict(a) if isinstance(a, dict) else a
            for a in raw_actions
        ]

        modifiers = dict(data.get("modifiers") or {})

        raw_attachments = data.get("attachments") or []
        attachments = [
            MediaAttachment.from_dict(att) if isinstance(att, dict) else att
            for att in raw_attachments
        ]

        intent = cls(actions=actions, modifiers=modifiers, attachments=attachments)

        # Handle legacy input keys if actions/modifiers weren't explicitly supplied
        if "max_words" in data and data["max_words"] is not None:
            intent.max_words = int(data["max_words"])
        if "min_words" in data and data["min_words"] is not None:
            intent.min_words = int(data["min_words"])
        if "history" in data and data["history"] is not None:
            intent.history = int(data["history"])
        if "file_use" in data and bool(data["file_use"]):
            intent.file_use = True
        if data.get("target_file"):
            intent.target_file = str(data["target_file"])
        if data.get("excluded_agents"):
            intent.excluded_agents = list(data["excluded_agents"])

        return intent


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
        mode = (
            raw_mode
            if raw_mode in (OrchestrationMode.SANDWICH.value, OrchestrationMode.FIXED.value)
            else OrchestrationMode.SANDWICH.value
        )
        if mode == OrchestrationMode.FIXED.value:
            fixed_config = dict(data.get("fixed_config") or {"chain": []})
            sandwich_config = {"start_chain": [], "end_chain": []}
        else:
            sandwich_config = dict(
                data.get("sandwich_config") or {"start_chain": [], "end_chain": []}
            )
            fixed_config = {"chain": []}

        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            mode=mode,
            description=str(data.get("description", "")),
            sandwich_config=sandwich_config,
            fixed_config=fixed_config,
        )

