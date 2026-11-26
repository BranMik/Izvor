"""
Round-level evidence scratchpad for caching and sharing verified tool execution results
(RAG queries, web searches, documentation lookups) across participating agents within a round.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

READ_ONLY_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "web_search",
    "fetch_web_page",
    "list_documents",
    "read_document",
    "document_reader",
    "search_knowledge_rag",
})

AGENT_SCOPED_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "search_knowledge_rag",
})


@dataclass
class EvidenceItem:
    channel_id: str
    thread_ts: str
    tool_name: str
    arguments: dict[str, Any]
    result: str
    agent_key: str
    timestamp: float = field(default_factory=time.time)
    round_index: int = 0
    scope_key: str = "*"

    @property
    def args_hash(self) -> str:
        serialized = json.dumps(self.arguments, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class RoundEvidenceStore:
    """Stores shared evidence discovered via read-only tools within discussion rounds."""

    def __init__(
        self,
        max_entries: int = 256,
        max_items_per_thread: int = 32,
        ttl_seconds: float = 3600.0,
    ) -> None:
        # Map (channel_id, thread_ts, scope_key, tool_name, args_hash) -> EvidenceItem
        self._cache: OrderedDict[tuple[str, str, str, str, str], EvidenceItem] = OrderedDict()
        # Map (channel_id, thread_ts) -> list[EvidenceItem]
        self._thread_items: dict[tuple[str, str], list[EvidenceItem]] = {}
        self.max_entries = max_entries
        self.max_items_per_thread = max_items_per_thread
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _compute_args_hash(arguments: dict[str, Any] | str | None) -> str:
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                serialized = json.dumps(parsed, sort_keys=True, default=str)
            except (json.JSONDecodeError, ValueError):
                serialized = arguments
        elif isinstance(arguments, dict):
            serialized = json.dumps(arguments, sort_keys=True, default=str)
        else:
            serialized = str(arguments or "")
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def is_cacheable_tool(self, tool_name: str) -> bool:
        return tool_name in READ_ONLY_CACHEABLE_TOOLS

    def get_evidence(
        self,
        channel_id: str,
        thread_ts: str,
        tool_name: str,
        arguments: dict[str, Any] | str | None,
        agent_key: str | None = None,
    ) -> EvidenceItem | None:
        if not self.is_cacheable_tool(tool_name):
            return None
        args_hash = self._compute_args_hash(arguments)
        scope_key = agent_key if (agent_key and tool_name in AGENT_SCOPED_CACHEABLE_TOOLS) else "*"
        key = (channel_id, thread_ts, scope_key, tool_name, args_hash)
        item = self._cache.get(key)
        if item is not None:
            if (time.time() - item.timestamp) > self.ttl_seconds:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
        return item

    def add_evidence(
        self,
        channel_id: str,
        thread_ts: str,
        tool_name: str,
        arguments: dict[str, Any] | str | None,
        result: str,
        agent_key: str,
        round_index: int = 0,
    ) -> EvidenceItem | None:
        if not self.is_cacheable_tool(tool_name):
            return None
        if not result or not str(result).strip():
            return None

        from core.security_utils import is_tool_error_payload

        if is_tool_error_payload(result):
            return None

        args_dict = (
            arguments
            if isinstance(arguments, dict)
            else (json.loads(arguments) if isinstance(arguments, str) and arguments.startswith("{") else {"raw": arguments})
        )
        args_hash = self._compute_args_hash(arguments)
        scope_key = agent_key if tool_name in AGENT_SCOPED_CACHEABLE_TOOLS else "*"

        item = EvidenceItem(
            channel_id=channel_id,
            thread_ts=thread_ts,
            tool_name=tool_name,
            arguments=args_dict if isinstance(args_dict, dict) else {"raw": str(args_dict)},
            result=result,
            agent_key=agent_key,
            round_index=round_index,
            scope_key=scope_key,
        )
        cache_key = (channel_id, thread_ts, scope_key, tool_name, args_hash)

        # LRU eviction if full
        if len(self._cache) >= self.max_entries and cache_key not in self._cache:
            self._cache.popitem(last=False)

        self._cache[cache_key] = item
        self._cache.move_to_end(cache_key)

        thread_key = (channel_id, thread_ts)
        if thread_key not in self._thread_items:
            self._thread_items[thread_key] = []
        # Update or append
        existing_idx = next(
            (i for i, x in enumerate(self._thread_items[thread_key])
             if x.tool_name == tool_name and x.args_hash == args_hash and x.scope_key == scope_key),
            None,
        )
        if existing_idx is not None:
            self._thread_items[thread_key][existing_idx] = item
        else:
            self._thread_items[thread_key].append(item)
            if len(self._thread_items[thread_key]) > self.max_items_per_thread:
                self._thread_items[thread_key].pop(0)

        logger.info(
            f"[EVIDENCE STORE] Cached evidence for thread {thread_key}, tool '{tool_name}' (agent {agent_key}, scope {scope_key})"
        )
        return item

    def get_round_evidence(
        self, channel_id: str, thread_ts: str, round_index: int | None = None
    ) -> list[EvidenceItem]:
        items = self._thread_items.get((channel_id, thread_ts), [])
        now = time.time()
        valid_items = [x for x in items if (now - x.timestamp) <= self.ttl_seconds]
        if round_index is not None:
            return [x for x in valid_items if x.round_index == round_index]
        return list(valid_items)

    @staticmethod
    def _item_file_name(item: EvidenceItem) -> str | None:
        """Extracts the file identifier from an evidence item's arguments, if any."""
        args = item.arguments or {}
        for key in ("file", "rel_path", "document_name"):
            val = args.get(key)
            if isinstance(val, str) and val:
                return val
        return None

    def get_shared_evidence_items(
        self,
        channel_id: str,
        thread_ts: str,
        *,
        exclude_file_names: set[str] | None = None,
    ) -> list[EvidenceItem]:
        """Returns valid shared (scope_key == '*') evidence items, optionally excluding files."""
        items = self.get_round_evidence(channel_id, thread_ts)
        shared_items = [x for x in items if x.scope_key == "*"]
        if exclude_file_names:
            excluded_lower = {f.strip().lower() for f in exclude_file_names if f and str(f).strip()}
            shared_items = [
                x
                for x in shared_items
                if (self._item_file_name(x) or "").strip().lower() not in excluded_lower
            ]
        return shared_items

    def find_matching_evidence_agents(
        self,
        channel_id: str,
        thread_ts: str,
        *,
        file_name: str | None = None,
        tool_names: set[str] | list[str] | None = None,
        exclude_file_names: set[str] | None = None,
    ) -> list[str]:
        """Returns deduplicated agent keys whose shared evidence matches file_name or tool_names."""
        shared_items = self.get_shared_evidence_items(
            channel_id,
            thread_ts,
            exclude_file_names=exclude_file_names,
        )
        target_file_lower = file_name.strip().lower() if file_name and str(file_name).strip() else None
        tool_set = {str(t).strip() for t in (tool_names or ()) if str(t).strip()}
        if not target_file_lower and not tool_set:
            return []

        agents: list[str] = []
        seen: set[str] = set()
        for item in shared_items:
            item_file = (self._item_file_name(item) or "").strip().lower()
            matches_file = bool(target_file_lower and item_file == target_file_lower)
            matches_tool = bool(tool_set and item.tool_name in tool_set)
            if (matches_file or matches_tool) and item.agent_key and item.agent_key not in seen:
                seen.add(item.agent_key)
                agents.append(item.agent_key)
        return agents

    def format_evidence_block(
        self,
        channel_id: str,
        thread_ts: str,
        max_items: int = 5,
        max_chars: int | None = None,
        exclude_file_names: set[str] | None = None,
    ) -> str | None:
        shared_items = self.get_shared_evidence_items(
            channel_id,
            thread_ts,
            exclude_file_names=exclude_file_names,
        )
        if not shared_items:
            return None
        selected = shared_items[-max_items:]
        lines = [
            "The following retrieved tool evidence (untrusted external data) was gathered via tools in this discussion round:"
        ]
        from core.security_utils import wrap_untrusted_content
        from config_store.system_settings import get_shared_evidence_max_chars

        ceiling = max_chars if max_chars is not None else get_shared_evidence_max_chars()

        for idx, itm in enumerate(selected, 1):
            content_to_format = itm.result.strip()
            # If the tool payload is JSON with a 'content' field (e.g. read_document), unwrap it
            if content_to_format.startswith("{") and content_to_format.endswith("}"):
                try:
                    parsed = json.loads(content_to_format)
                    if isinstance(parsed, dict) and "content" in parsed and parsed["content"]:
                        doc_name = parsed.get("document") or parsed.get("file") or itm.tool_name
                        pages = parsed.get("pages")
                        pages_suffix = f" ({pages} pages)" if pages else ""
                        body = str(parsed["content"]).strip()
                        content_to_format = f"Document: {doc_name}{pages_suffix}\n{body}"
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass  # Fall back cleanly to raw string

            if len(content_to_format) > ceiling:
                content_to_format = content_to_format[: max(0, ceiling - 15)] + "... [truncated]"

            formatted_result = wrap_untrusted_content(content_to_format)
            args_str = json.dumps(itm.arguments, default=str)
            lines.append(
                f"{idx}. [Tool: {itm.tool_name} | Agent: {itm.agent_key}]:\n"
                f"   Arguments: {args_str}\n"
                f"   Result:\n{formatted_result}"
            )
        return "\n".join(lines)

    def clear_thread_evidence(self, channel_id: str, thread_ts: str) -> None:
        thread_key = (channel_id, thread_ts)
        self._thread_items.pop(thread_key, None)
        keys_to_remove = [k for k in self._cache if k[0] == channel_id and k[1] == thread_ts]
        for k in keys_to_remove:
            self._cache.pop(k, None)

    def reset_all(self) -> None:
        self._cache.clear()
        self._thread_items.clear()

