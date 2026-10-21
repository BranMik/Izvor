"""
Round-level evidence scratchpad for caching and sharing verified tool execution results
(RAG queries, web searches, documentation lookups) across participating agents within a round.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

READ_ONLY_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "query_knowledge_base",
    "search_knowledge_base",
    "duckduckgo_search",
    "search_web",
    "fetch_web_page",
    "web_scrape",
    "read_document",
    "inspect_document",
    "fetch_file",
    "read_codebase",
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

    @property
    def args_hash(self) -> str:
        serialized = json.dumps(self.arguments, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class RoundEvidenceStore:
    """Stores shared evidence discovered via read-only tools within discussion rounds."""

    def __init__(self) -> None:
        # Map (channel_id, thread_ts, tool_name, args_hash) -> EvidenceItem
        self._cache: dict[tuple[str, str, str, str], EvidenceItem] = {}
        # Map (channel_id, thread_ts) -> list[EvidenceItem]
        self._thread_items: dict[tuple[str, str], list[EvidenceItem]] = {}

    @staticmethod
    def _compute_args_hash(arguments: dict[str, Any] | str | None) -> str:
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                serialized = json.dumps(parsed, sort_keys=True, default=str)
            except Exception:
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
    ) -> EvidenceItem | None:
        args_hash = self._compute_args_hash(arguments)
        key = (channel_id, thread_ts, tool_name, args_hash)
        return self._cache.get(key)

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
        args_dict = (
            arguments
            if isinstance(arguments, dict)
            else (json.loads(arguments) if isinstance(arguments, str) and arguments.startswith("{") else {"raw": arguments})
        )
        args_hash = self._compute_args_hash(arguments)
        item = EvidenceItem(
            channel_id=channel_id,
            thread_ts=thread_ts,
            tool_name=tool_name,
            arguments=args_dict if isinstance(args_dict, dict) else {"raw": str(args_dict)},
            result=result,
            agent_key=agent_key,
            round_index=round_index,
        )
        cache_key = (channel_id, thread_ts, tool_name, args_hash)
        self._cache[cache_key] = item

        thread_key = (channel_id, thread_ts)
        if thread_key not in self._thread_items:
            self._thread_items[thread_key] = []
        # Update or append
        existing_idx = next(
            (i for i, x in enumerate(self._thread_items[thread_key])
             if x.tool_name == tool_name and x.args_hash == args_hash),
            None,
        )
        if existing_idx is not None:
            self._thread_items[thread_key][existing_idx] = item
        else:
            self._thread_items[thread_key].append(item)

        logger.info(
            f"[EVIDENCE STORE] Cached evidence for thread {thread_key}, tool '{tool_name}' (agent {agent_key})"
        )
        return item

    def get_round_evidence(
        self, channel_id: str, thread_ts: str, round_index: int | None = None
    ) -> list[EvidenceItem]:
        items = self._thread_items.get((channel_id, thread_ts), [])
        if round_index is not None:
            return [x for x in items if x.round_index == round_index]
        return list(items)

    def format_evidence_block(self, channel_id: str, thread_ts: str, max_items: int = 5) -> str | None:
        items = self.get_round_evidence(channel_id, thread_ts)
        if not items:
            return None
        selected = items[-max_items:]
        lines = [
            "The following verified evidence was gathered via tools in this discussion round:"
        ]
        for idx, itm in enumerate(selected, 1):
            res_preview = itm.result.strip()
            if len(res_preview) > 600:
                res_preview = res_preview[:590] + "... [truncated]"
            args_str = json.dumps(itm.arguments, default=str)
            lines.append(
                f"{idx}. [Tool: {itm.tool_name} | Agent: {itm.agent_key}]:\n"
                f"   Arguments: {args_str}\n"
                f"   Result: {res_preview}"
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
