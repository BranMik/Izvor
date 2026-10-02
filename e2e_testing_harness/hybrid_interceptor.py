"""
Hybrid LLM & Tool Interceptor for E2E Simulation.

Executes live LLMs for system-level inference (IntentExtractor, LanguageDetector)
while generating controlled, structured responses for verbose debate turns and
supplying deterministic fixtures for external web searches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from unittest.mock import MagicMock

import litellm

logger = logging.getLogger("e2e_testing_harness.interceptor")

# Deterministic fixture results for external web tools
MOCK_WEB_SEARCH_FIXTURES: dict[str, list[dict[str, str]]] = {
    "default": [
        {
            "title": "Anthropic Claude API Pricing & Model Benchmarks 2025",
            "url": "https://anthropic.com/pricing",
            "snippet": "Claude 3.7 Sonnet pricing: $3.00 per million input tokens, $15.00 per million output tokens. Context window: 200k tokens.",
        },
        {
            "title": "OpenAI API Pricing Comparison 2025",
            "url": "https://openai.com/api/pricing",
            "snippet": "GPT-4o pricing: $2.50 per million input tokens, $10.00 per million output tokens. Fast multimodal capabilities.",
        },
    ],
    "vector": [
        {
            "title": "Top Vector Databases in 2025: Pinecone, Qdrant, Milvus Comparison",
            "url": "https://db-engines.com/vector-databases",
            "snippet": "Pinecone offers managed cloud vector search. Qdrant provides open-source Rust-based vector search with payload filtering. Milvus excels at massive billion-scale vector indexing.",
        },
    ],
}


class MockModelResponse:
    """Mock LiteLLM completion response object matching litellm.ModelResponse interface."""

    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[Any] | None = None,
        model: str = "mock-model",
        prompt_tokens: int = 150,
        completion_tokens: int = 45,
    ) -> None:
        self.choices = [MockChoice(content=content, tool_calls=tool_calls)]
        self.model = model
        self.usage = MockUsage(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )


class MockChoice:
    def __init__(
        self, content: str | None, tool_calls: list[Any] | None = None
    ) -> None:
        self.message = MockMessage(content=content, tool_calls=tool_calls)


class MockMessage:
    def __init__(
        self, content: str | None, tool_calls: list[Any] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.role = "assistant"

    def model_dump(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return d


class MockToolCall:
    def __init__(
        self, call_id: str, name: str, arguments: dict[str, Any] | str
    ) -> None:
        self.id = call_id
        self.type = "function"
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = (
            json.dumps(arguments) if isinstance(arguments, dict) else arguments
        )


class MockUsage:
    def __init__(self, prompt_tokens: int = 150, completion_tokens: int = 45) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class HybridInterceptor:
    """Controls the routing of litellm.acompletion and external tool calls."""

    def __init__(
        self,
        allow_live_system_llm: bool = True,
        allow_all_live_llm: bool = True,
    ) -> None:
        self.allow_live_system_llm = allow_live_system_llm
        self.allow_all_live_llm = allow_all_live_llm
        self.real_acompletion = litellm.acompletion
        self.real_completion = litellm.completion
        self.call_history: list[dict[str, Any]] = []

    def intercept_completion(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.allow_all_live_llm:
            return self.real_completion(*args, **kwargs)
        try:
            return asyncio.run(self.intercept_acompletion(*args, **kwargs))
        except RuntimeError:
            # Event loop already running in current thread, create separate thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run, self.intercept_acompletion(*args, **kwargs)
                )
                return future.result()

    async def intercept_acompletion(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.call_history.append(kwargs)
        if self.allow_all_live_llm:
            return await self.real_acompletion(*args, **kwargs)

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", "")
        tools = kwargs.get("tools", [])

        # 1. Identify System Agents (IntentExtractor, LanguageDetector)
        is_intent_extractor = any(
            "intent, directive, and parameter extractor"
            in str(m.get("content", "")).lower()
            for m in messages
            if m.get("role") == "system"
        )
        is_language_detector = any(
            "identify the primary natural human language"
            in str(m.get("content", "")).lower()
            or "language classifier" in str(m.get("content", "")).lower()
            for m in messages
            if m.get("role") == "system"
        )

        is_scribe = any(
            "discussion scribe" in str(m.get("content", "")).lower()
            or "strategic synthesizer" in str(m.get("content", "")).lower()
            for m in messages
            if m.get("role") == "system"
        )

        # Run Live LLM for System Agents if keys exist and live is allowed
        if (
            is_intent_extractor or is_language_detector or is_scribe
        ) and self.allow_live_system_llm:
            try:
                # Test if model is executable
                return await self.real_acompletion(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - Hybrid interceptor falls back to mock extractor if live LLM fails
                logger.warning(
                    f"[HYBRID] Live system LLM failed ({e}), using deterministic extractor."
                )

        if is_scribe:
            sys_text = ""
            for m in messages:
                if m.get("role") == "system":
                    sys_text = str(m.get("content", ""))
                    break

            max_words_m = re.search(
                r"NOT exceed\s*(\d+)\s*words", sys_text, re.IGNORECASE
            )
            min_words_m = re.search(
                r"minimum length of\s*(\d+)\s*words", sys_text, re.IGNORECASE
            )
            max_words = int(max_words_m.group(1)) if max_words_m else None
            min_words = int(min_words_m.group(1)) if min_words_m else None

            if max_words and max_words <= 45:
                content = (
                    "📋 *Strategic Synthesis & Action Plan*\n"
                    "🎯 *Core Objective*\n"
                    "Evaluate distributed transaction consistency in microservices.\n\n"
                    "🤝 *Consensus*\n"
                    "Adopt saga pattern with event streaming."
                )
            elif min_words and min_words >= 25:
                content = (
                    "📋 *Strategic Synthesis & Action Plan*\n"
                    "🎯 *Core Objective & Problem Statement*\n"
                    "Analyze event sourcing replay guarantees, partition offsets, and consumer group rebalancing across high-throughput Kafka clusters.\n\n"
                    "💡 *Key Perspectives & Insights*\n"
                    "• Security Architect emphasized partition offset retention, idempotency, and consumer rebalancing protocols.\n"
                    "• Exactly-once semantics are required to prevent duplicate state mutations.\n\n"
                    "🚀 *Action Items & Next Steps*\n"
                    "• Configure automated consumer rebalancing tests."
                )
            else:
                content = (
                    "📋 *Strategic Synthesis & Action Plan*\n"
                    "🎯 *Core Objective*\n"
                    "Review architecture and event streaming consistency.\n\n"
                    "💡 *Key Perspectives*\n"
                    "• Decoupling pipelines provides resilience and fault tolerance.\n\n"
                    "🚀 *Next Steps*\n"
                    "• Document integration patterns."
                )
            return MockModelResponse(
                content=content,
                model=model,
                prompt_tokens=150,
                completion_tokens=len(content.split()),
            )

        if is_language_detector:
            # Deterministic language detection fallback
            from nlp.language_detector import clean_system_and_tags

            user_text = messages[-1].get("content", "") if messages else ""
            clean_user = clean_system_and_tags(user_text)
            croatian_words = [
                "koji",
                "su",
                "kljucni",
                "ključni",
                "rizici",
                "rizik",
                "ako",
                "migriramo",
                "rijeci",
                "riječi",
                "poruke",
                "sustav",
                "sustava",
                "servis",
                "servisa",
                "tehnički",
                "tehnicki",
                "pristup",
                "stručnjak",
                "strucnjak",
                "bez",
                "prekini",
            ]
            has_croatian = any(w in clean_user.lower() for w in croatian_words)
            detected = "Croatian" if has_croatian else "English"
            return MockModelResponse(
                content=detected, model=model, prompt_tokens=40, completion_tokens=5
            )

        is_list_skills = any(
            "current platform skills directory:" in str(m.get("content", "")).lower()
            for m in messages
        )
        if is_list_skills:
            return MockModelResponse(
                content="Here is the Skills directory:\n- web_research\n- codebase_inspection",
                model=model,
                prompt_tokens=150,
                completion_tokens=20,
            )

        if is_intent_extractor:
            # Deterministic intent extractor fallback
            user_text = messages[-1].get("content", "") if messages else ""
            extracted_modifiers: dict[str, Any] = {}
            extracted_actions: list[dict[str, Any]] = []

            m_words = re.search(
                r"(?:max|maksimalno)\s*(\d+)\s*(?:rije[cč]i|words)",
                user_text,
                re.IGNORECASE,
            )
            if m_words:
                extracted_modifiers["max_words"] = int(m_words.group(1))

            min_words = re.search(
                r"(?:min|minimalno|barem)\s*(\d+)\s*(?:rije[cč]i|words)",
                user_text,
                re.IGNORECASE,
            )
            if min_words:
                extracted_modifiers["min_words"] = int(min_words.group(1))

            hist = re.search(
                r"(?:zadnje|last)\s*(\d+)\s*(?:poruke|messages)",
                user_text,
                re.IGNORECASE,
            )
            if hist:
                extracted_modifiers["history"] = int(hist.group(1))

            from pipeline.entity_resolver import entity_resolver
            tagged = entity_resolver.extract_tagged_agents(user_text)
            for ag in tagged:
                resolved = entity_resolver.resolve_agent_name(ag)
                if resolved:
                    extracted_actions.append({"name": resolved, "params": {}})

            content_json = json.dumps(
                {
                    "modifiers": extracted_modifiers,
                    "actions": extracted_actions,
                }
            )
            return MockModelResponse(
                content=content_json,
                model=model,
                prompt_tokens=80,
                completion_tokens=25,
            )

        # 2. Agent Turn Execution: Tool reasoning vs Verbose discussion text
        last_message = messages[-1] if messages else {}
        last_role = last_message.get("role", "")
        last_content = str(last_message.get("content", ""))

        # Check if model should perform tool calls (progressive disclosure)
        has_tool_schemas = bool(tools)
        has_tool_results = last_role == "tool"

        # Did user request web search?
        user_prompt_str = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_prompt_str = str(m.get("content", ""))
                break

        needs_web_search = any(
            k in user_prompt_str.lower()
            for k in [
                "search the web",
                "pricing",
                "anthropic",
                "competitor",
                "web search",
                "vector",
            ]
        )
        needs_codebase = any(
            k in user_prompt_str.lower()
            for k in ["inspect", "directory layout", "codebase", "turn_pipeline.py"]
        )
        needs_doc_authoring = any(
            k in user_prompt_str.lower()
            for k in [
                "draft",
                "business case",
                "save it as a document",
                "llm_cost_reduction_plan.md",
            ]
        )
        needs_file_read = any(
            k in user_prompt_str.lower()
            for k in ["read_document", "sys_spec.txt", "read document", "throughput"]
        )
        needs_unauthorized_tool = any(
            k in user_prompt_str.lower()
            for k in [
                "load_skill workspace_authoring",
                "modify system configuration",
                "unauthorized",
            ]
        )

        # First turn of tool loop: trigger tool call
        if has_tool_schemas and not has_tool_results:
            tool_names = [t.get("function", {}).get("name") for t in tools]

            # Step A: Progressive disclosure load_skill
            if "load_skill" in tool_names:
                if needs_unauthorized_tool:
                    tc = MockToolCall(
                        "call_unauth",
                        "load_skill",
                        {"skill_name": "workspace_authoring"},
                    )
                    return MockModelResponse(content=None, tool_calls=[tc], model=model)
                elif needs_file_read:
                    tc = MockToolCall(
                        "call_load_doc_reader",
                        "load_skill",
                        {"skill_name": "document_reader"},
                    )
                    return MockModelResponse(content=None, tool_calls=[tc], model=model)
                elif needs_web_search:
                    tc = MockToolCall(
                        "call_load_web", "load_skill", {"skill_name": "web_research"}
                    )
                    return MockModelResponse(content=None, tool_calls=[tc], model=model)
                elif needs_codebase:
                    tc = MockToolCall(
                        "call_load_doc_reader",
                        "load_skill",
                        {"skill_name": "document_reader"},
                    )
                    return MockModelResponse(content=None, tool_calls=[tc], model=model)
                elif needs_doc_authoring:
                    tc = MockToolCall(
                        "call_load_doc",
                        "load_skill",
                        {"skill_name": "document_authoring"},
                    )
                    return MockModelResponse(content=None, tool_calls=[tc], model=model)

            # Step B: Low-level tool calling
            if "read_document" in tool_names and needs_file_read:
                tc = MockToolCall(
                    "call_read_doc", "read_document", {"rel_path": "sys_spec.txt"}
                )
                return MockModelResponse(content=None, tool_calls=[tc], model=model)

            if "web_search" in tool_names and needs_web_search:
                q = (
                    "vector databases"
                    if "vector" in user_prompt_str.lower()
                    else "Anthropic Claude pricing 2025"
                )
                tc = MockToolCall("call_web_search", "web_search", {"query": q})
                return MockModelResponse(content=None, tool_calls=[tc], model=model)

            if "list_documents" in tool_names and needs_codebase:
                tc = MockToolCall(
                    "call_list_dir",
                    "list_documents",
                    {"rel_path": "orchestration/stages"},
                )
                return MockModelResponse(content=None, tool_calls=[tc], model=model)

            if "write_document" in tool_names and needs_doc_authoring:
                tc = MockToolCall(
                    "call_write_doc",
                    "write_document",
                    {
                        "rel_path": "llm_cost_reduction_plan.md",
                        "content": "# LLM Cost Reduction Plan\n\n1. Cache prompt embeddings\n2. Use smaller tier models for intent extraction\n3. Set strict circuit breaker budgets.",
                    },
                )
                return MockModelResponse(content=None, tool_calls=[tc], model=model)

        # 3. Final Discussion Response (concise persona response, respecting word counts and nominations)
        system_content = ""
        for m in messages:
            if m.get("role") == "system":
                system_content = str(m.get("content", ""))
                break

        # Check persona and language
        is_croatian = (
            "croatian" in system_content.lower()
            or "hrvatskom" in system_content.lower()
            or any(
                "strictly in croatian" in str(m.get("content", "")).lower()
                for m in messages
            )
            or any("hrvatskom" in str(m.get("content", "")).lower() for m in messages)
        )

        # Check for word constraints in system prompt or messages
        max_words_match = re.search(
            r"(?:MAXIMUM WORD COUNT|strictly NOT exceed|max_words).*?(\d+)",
            system_content,
            re.IGNORECASE,
        )
        max_limit = int(max_words_match.group(1)) if max_words_match else None

        min_words_match = re.search(
            r"(?:MINIMUM WORD COUNT|minimum length of|min_words).*?(\d+)",
            system_content,
            re.IGNORECASE,
        )
        min_limit = int(min_words_match.group(1)) if min_words_match else None

        if not min_limit:
            u_min = re.search(
                r"/(?:scribe_)?min_words\s+(\d+)", user_prompt_str, re.IGNORECASE
            )
            if u_min:
                min_limit = int(u_min.group(1))
        if not max_limit:
            u_max = re.search(
                r"/(?:scribe_)?max_words\s+(\d+)", user_prompt_str, re.IGNORECASE
            )
            if u_max:
                max_limit = int(u_max.group(1))

        # Check if this prompt warrants a nomination (e.g. Sandwich middle pool, or asked to nominate)
        warrants_nomination = (
            "nominate" in user_prompt_str.lower()
            or "[innovator]" in user_prompt_str.lower()
            or "who speaks next" in user_prompt_str.lower()
        )

        nomination_tag = ""
        if warrants_nomination:
            if "sceptic" in user_prompt_str.lower():
                nomination_tag = " [LegalAdvisor]"
            else:
                nomination_tag = " [Sceptic]"

        # Craft concise mock text
        if is_croatian:
            base_text = (
                f"Kao tehnički stručnjak, smatram da je modularni pristup temelj stabilnosti našeg sustava. "
                f"Potrebno je osigurati odgovarajuću izolaciju servisa, definirati ugovore i pratiti latenciju.{nomination_tag}"
            )
        elif (
            "Permission denied" in last_content or "not authorized" in last_content
        ) and has_tool_results:
            base_text = (
                f"I attempted to execute the action, but received an authorization error: {last_content}. "
                f"I am not authorized to load the requested skill in this thread.{nomination_tag}"
            )
        elif needs_file_read and has_tool_results:
            base_text = (
                f"According to 'resources/e2e_testing/sys_spec.txt', the system throughput is specified at 5000 rps "
                f"with latency target <50ms under peak load.{nomination_tag}"
            )
        elif needs_web_search and has_tool_results:
            if "vector" in user_prompt_str.lower():
                base_text = (
                    f"Based on our web search analysis for vector databases:\n"
                    f"• Pinecone provides a fully-managed cloud vector indexing service.\n"
                    f"• Qdrant offers high-performance Rust-based vector search with advanced payload filtering.\n"
                    f"• Milvus is optimized for massive billion-scale vector operations.{nomination_tag}"
                )
            else:
                base_text = (
                    f"Based on current official pricing from Anthropic and OpenAI:\n"
                    f"• Claude 3.7 Sonnet: $3.00 / MTok input, $15.00 / MTok output (Source: https://anthropic.com/pricing)\n"
                    f"• GPT-4o: $2.50 / MTok input, $10.00 / MTok output (Source: https://openai.com/api/pricing){nomination_tag}"
                )
        elif needs_codebase and has_tool_results:
            base_text = (
                f"The 'orchestration/stages' directory includes validation.py, resolution.py, context_prep.py, "
                f"execution.py, post_record.py, and tool_status.py. In 'orchestration/turn_pipeline.py', the TurnPipeline "
                f"integrates ValidationStageMixin, ResolutionStageMixin, ContextPrepStageMixin, ExecutionStageMixin, and PostRecordStageMixin.{nomination_tag}"
            )
        elif needs_doc_authoring and has_tool_results:
            base_text = (
                f"I have drafted the 3-point business case and successfully saved it to 'resources/e2e_testing/llm_cost_reduction_plan.md'. "
                f"The document details prompt caching, tier optimization, and governance thresholds.{nomination_tag}"
            )
        else:
            base_text = (
                f"From an architectural standpoint, decoupling our pipeline into asynchronous event streams provides fault tolerance "
                f"and elastic horizontal scalability under peak load.{nomination_tag}"
            )

        if min_limit and min_limit >= 10:
            # Expand to satisfy floor
            filler = " We must evaluate observability, distributed tracing, backpressure mitigation, partition strategies, and schema registries."
            while len(base_text.split()) < min_limit:
                base_text += filler

        if max_limit and len(base_text.split()) > max_limit:
            words = base_text.split()[:max_limit]
            base_text = " ".join(words)
            if nomination_tag and nomination_tag.strip() not in base_text:
                base_text = base_text[: -len(nomination_tag)] + nomination_tag

        return MockModelResponse(
            content=base_text,
            model=model,
            prompt_tokens=320,
            completion_tokens=len(base_text.split()),
        )


async def mock_execute_mcp_tool(tool_name: str, arguments: dict[str, Any] | str) -> str:
    """Provides deterministic fixture execution for external tools."""
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if tool_name == "web_search":
        query = str(args.get("query", "default")).lower()
        if "vector" in query:
            results = MOCK_WEB_SEARCH_FIXTURES["vector"]
        else:
            results = MOCK_WEB_SEARCH_FIXTURES.get(
                query, MOCK_WEB_SEARCH_FIXTURES["default"]
            )
        return json.dumps({"results": results})
    elif tool_name == "fetch_web_page":
        return "Anthropic Claude 3.7 Sonnet pricing: $3.00/MTok input, $15.00/MTok output. Fast reasoning capabilities."

    # For local filesystem/doc tools, run the real implementation
    from services.mcp_tools import execute_mcp_tool

    return execute_mcp_tool(tool_name, arguments)
