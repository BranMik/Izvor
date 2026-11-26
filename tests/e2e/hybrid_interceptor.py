"""
Hybrid LLM & Tool Interceptor for E2E Simulation.

Supports offline simulation (`--mock`) and hybrid mocked LLM (`--hybrid-mocked-llm`)
by simulating IntentExtractor, LanguageDetector, InternalRevisor, DiscussionScribe,
Challenger, Arbitrator, multi-step progressive-disclosure tool loops, and control-plane tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from unittest.mock import MagicMock

import litellm

logger = logging.getLogger("tests.e2e.interceptor")

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
            "title": "Top Vector Databases & Pricing 2025: Pinecone, Qdrant, Milvus",
            "url": "https://db-engines.com/vector-databases",
            "snippet": "Pinecone managed serverless starts around $0.08/GB-month. Qdrant Cloud and Milvus offer flexible open-source and usage-based pricing for vector indexing.",
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
        self.force_empty_scribe_output: bool = False
        self.simulated_turn_delay_seconds: float = 0.0

    def reset_flags(self) -> None:
        self.force_empty_scribe_output = False
        self.simulated_turn_delay_seconds = 0.0

    def intercept_completion(self, *args: Any, **kwargs: Any) -> Any:
        if self.allow_all_live_llm:
            return self.real_completion(*args, **kwargs)
        try:
            return asyncio.run(self.intercept_acompletion(*args, **kwargs))
        except RuntimeError:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run, self.intercept_acompletion(*args, **kwargs)
                )
                return future.result()

    async def intercept_acompletion(self, *args: Any, **kwargs: Any) -> Any:
        self.call_history.append(kwargs)
        if self.allow_all_live_llm:
            return await self.real_acompletion(*args, **kwargs)

        if self.simulated_turn_delay_seconds > 0:
            await asyncio.sleep(self.simulated_turn_delay_seconds)

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", "")
        tools = kwargs.get("tools") or []

        sys_text_all = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        sys_lower = sys_text_all.lower()

        # 1. Identify System Agents
        is_intent_extractor = any(
            k in sys_lower
            for k in (
                "intent, directive, and parameter extractor",
                "structured directive & parameter extractor",
                'id="intent_extractor"',
                "intent_extractor",
            )
        )
        is_language_detector = any(
            k in sys_lower
            for k in (
                "identify the primary natural human language",
                "language classifier",
                'id="language_detector"',
                "language_detector",
            )
        )
        is_internal_revisor = any(
            k in sys_lower
            for k in (
                'id="internalrevisor"',
                "factual consistency & hallucination verifier",
                "internalrevisor",
            )
        )
        is_scribe = any(
            k in sys_lower
            for k in (
                "discussion scribe",
                "strategic synthesizer",
                "discussion_scribe",
                "live strategic synthesis",
                'id="discussion_scribe_live"',
                'id="discussion_scribe_conclusion"',
                "dialectical conclusion synthesizer",
            )
        )
        is_challenger = (
            'id="challenger"' in sys_lower
            or "dialectical stress-test challenger" in sys_lower
        )
        is_arbitrator = (
            'id="arbitrator"' in sys_lower
            or 'role="arbitrator"' in sys_lower
        )

        # Run Live LLM for System Agents if hybrid live system mode is allowed
        if (
            is_intent_extractor
            or is_language_detector
            or is_scribe
            or is_internal_revisor
        ) and self.allow_live_system_llm:
            try:
                return await self.real_acompletion(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[HYBRID] Live system LLM failed (%s), using deterministic mock.",
                    e,
                )

        if is_internal_revisor:
            user_msg_text = str(messages[-1].get("content", "")) if messages else ""
            is_stage2 = "<stage1_preliminary_flag>" in user_msg_text
            # Hardened mock: only flag "99999" when contradicting ground truth ("5000") is actually
            # present inside <retrieved_evidence>, so missing-evidence regressions are not masked.
            evidence_match = re.search(
                r"<retrieved_evidence>(.*?)</retrieved_evidence>",
                user_msg_text,
                re.DOTALL | re.IGNORECASE,
            )
            has_ground_truth = bool(evidence_match and "5000" in evidence_match.group(1))
            cited_match = re.search(
                r"<cited_prior_statements[^>]*>(.*?)</cited_prior_statements>",
                user_msg_text,
                re.DOTALL | re.IGNORECASE,
            )
            has_cited_visionary = bool(cited_match and "visionary" in cited_match.group(1).lower())
            has_peer_misattribution = (
                "abandon multi-region failover" in user_msg_text.lower()
                and has_cited_visionary
            )
            if is_stage2:
                if has_peer_misattribution:
                    return MockModelResponse(
                        content=(
                            'ATTENTION: Agent [Tactician] hallucinated the following fact: '
                            '"As Visionary stated, we must abandon multi-region failover completely and deploy everything on a single bare-metal server.". '
                            "The correct information based on the original context is: "
                            "Visionary recommended multi-region active-active failover, not abandoning it."
                        ),
                        model=model,
                        prompt_tokens=120,
                        completion_tokens=32,
                    )
                if (
                    "99999" in user_msg_text
                    and "FALSE_POSITIVE_TRIGGER" not in user_msg_text
                    and has_ground_truth
                ):
                    return MockModelResponse(
                        content=(
                            'ATTENTION: Agent [SecurityArchitect] hallucinated the following fact: '
                            '"99999 rps". The correct information based on the original context is: '
                            "throughput is 5000 rps."
                        ),
                        model=model,
                        prompt_tokens=120,
                        completion_tokens=28,
                    )
                return MockModelResponse(
                    content="NO_HALLUCINATION",
                    model=model,
                    prompt_tokens=120,
                    completion_tokens=4,
                )

            if has_peer_misattribution:
                return MockModelResponse(
                    content=(
                        'ATTENTION: Agent [Tactician] hallucinated the following fact: '
                        '"As Visionary stated, we must abandon multi-region failover completely and deploy everything on a single bare-metal server.". '
                        "The correct information based on the original context is: "
                        "Visionary recommended multi-region active-active failover, not abandoning it."
                    ),
                    model=model,
                    prompt_tokens=120,
                    completion_tokens=32,
                )

            if "FALSE_POSITIVE_TRIGGER" in user_msg_text or (
                "99999" in user_msg_text and has_ground_truth
            ):
                return MockModelResponse(
                    content=(
                        'ATTENTION: Agent [SecurityArchitect] hallucinated the following fact: '
                        '"99999 rps". The correct information based on the original context is: '
                        "throughput is 5000 rps."
                    ),
                    model=model,
                    prompt_tokens=120,
                    completion_tokens=28,
                )
            return MockModelResponse(
                content="NO_HALLUCINATION",
                model=model,
                prompt_tokens=100,
                completion_tokens=4,
            )

        if is_scribe:
            if self.force_empty_scribe_output:
                return MockModelResponse(
                    content="",
                    model=model,
                    prompt_tokens=100,
                    completion_tokens=0,
                )

            max_words_m = re.search(
                r"NOT exceed\s*(\d+)\s*words", sys_text_all, re.IGNORECASE
            )
            min_words_m = re.search(
                r"minimum length of\s*(\d+)\s*words", sys_text_all, re.IGNORECASE
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
            from nlp.language_detector import clean_system_and_tags

            user_text = messages[-1].get("content", "") if messages else ""
            clean_user = clean_system_and_tags(user_text)
            croatian_words = (
                "koji", "su", "kljucni", "ključni", "rizici", "rizik", "ako",
                "migriramo", "rijeci", "riječi", "poruke", "sustav", "sustava",
                "servis", "servisa", "tehnički", "tehnicki", "pristup",
                "stručnjak", "strucnjak", "bez", "prekini", "arhitektura",
                "molim", "objasni", "kako",
            )
            has_croatian = any(
                re.search(rf"\b{re.escape(w)}\b", clean_user.lower())
                for w in croatian_words
            )
            detected = "Croatian" if has_croatian else "English"
            return MockModelResponse(
                content=detected, model=model, prompt_tokens=40, completion_tokens=5
            )

        if is_intent_extractor:
            user_text = messages[-1].get("content", "") if messages else ""
            u_lower = user_text.lower()
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

            wants_web = any(
                k in u_lower
                for k in ("search the web", "web search", "pricing", "anthropic", "vector database")
            )
            wants_doc_read = any(
                k in u_lower
                for k in ("read ", "sys_spec.txt", "compliance_guidelines.txt", "summarize the")
            )
            wants_doc_write = any(
                k in u_lower
                for k in ("save it as a document", "write_document", "llm_cost_reduction_plan.md")
            )

            if wants_web:
                extracted_actions.append({"name": "web_research", "params": {}})
            if wants_doc_read:
                file_m = re.search(r"([A-Za-z0-9_.-]+\.(?:txt|md|pdf|csv|json))", user_text, re.IGNORECASE)
                doc_params = {"target_file": file_m.group(1)} if file_m else {}
                extracted_actions.append({"name": "document_reader", "params": doc_params})
            if wants_doc_write:
                extracted_actions.append({"name": "document_authoring", "params": {}})

            croatian_markers = (
                "koji", "su", "ključni", "kljucni", "rizici", "sustav", "arhitektura",
                "kako", "molim", "objasni", "migriramo",
            )
            detected_lang = (
                "Croatian"
                if any(re.search(rf"\b{re.escape(w)}\b", u_lower) for w in croatian_markers)
                else "English"
            )
            is_pure_skill = bool(extracted_actions) and not any(
                k in u_lower
                for k in ("debate", "critique", "compare strategies", "brainstorm", "evaluate")
            )

            content_json = json.dumps(
                {
                    "modifiers": extracted_modifiers,
                    "actions": extracted_actions,
                    "language": detected_lang,
                    "is_pure_skill_task": is_pure_skill,
                }
            )
            return MockModelResponse(
                content=content_json,
                model=model,
                prompt_tokens=80,
                completion_tokens=25,
            )

        # 2. Agent Turn Execution: Track tools already executed in this turn
        executed_tools: list[str] = []
        loaded_skills: set[str] = set()
        tool_results_text: list[str] = []

        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    if fn.get("name") == "load_skill":
                        try:
                            args_obj = json.loads(fn.get("arguments") or "{}")
                            if args_obj.get("skill_name"):
                                loaded_skills.add(str(args_obj["skill_name"]))
                        except (json.JSONDecodeError, TypeError):
                            pass
            elif m.get("role") == "tool":
                t_name = m.get("name") or ""
                if t_name:
                    executed_tools.append(t_name)
                tool_results_text.append(str(m.get("content", "")))

        joined_tool_results = "\n".join(tool_results_text)
        has_tool_schemas = bool(tools)
        has_tool_results = bool(executed_tools)

        # Find latest user message and full user prompt context across turns
        user_prompt_str = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_prompt_str = str(m.get("content", ""))
                break
        prompt_lower = user_prompt_str.lower()
        all_user_lower = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        ).lower()

        # Check HITL pause notice from ExecutionStage
        if "tool execution paused:" in joined_tool_results.lower():
            return MockModelResponse(
                content=(
                    "Execution is paused pending human approval in Slack for the requested destructive tool. "
                    "Please click Approve or Deny on the approval card."
                ),
                model=model,
                prompt_tokens=220,
                completion_tokens=25,
            )

        needs_web_search = any(
            k in prompt_lower
            for k in (
                "search the web",
                "pricing",
                "anthropic",
                "competitor",
                "web search",
                "vector",
            )
        )
        needs_doc_authoring = any(
            k in prompt_lower
            for k in (
                "save it as a document",
                "llm_cost_reduction_plan.md",
                "write_document",
            )
        )
        needs_file_read = any(
            k in all_user_lower
            for k in (
                "read_document",
                "read_file",
                "sys_spec.txt",
                "compliance_guidelines.txt",
                "read document",
                "throughput",
                "retention obligations",
            )
        ) and not is_internal_revisor
        needs_unauthorized_tool = any(
            k in prompt_lower
            for k in (
                "document_authoring",
                "save this configuration",
                "modify system configuration",
                "unauthorized",
            )
        ) and not needs_doc_authoring

        needs_clarification = any(
            k in prompt_lower
            for k in ("request_user_clarification", "ask the user for clarification", "clarify requirements")
        )
        needs_convergence = any(
            k in all_user_lower
            for k in ("signal_consensus_convergence", "signal consensus", "converge on")
        )
        needs_mcp = "mcp" in prompt_lower or any(
            t.get("function", {}).get("name", "").startswith("mcp__") for t in tools
        )

        # Multi-step Tool Calling Loop (Step A: load_skill -> Step B: low-level tool)
        if has_tool_schemas:
            tool_names = [t.get("function", {}).get("name") for t in tools]

            # Arbitrator adjudication flow
            if is_arbitrator:
                if "load_skill" in tool_names and "control_plane" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_arb_cp", "load_skill", {"skill_name": "control_plane"})
                        ],
                        model=model,
                    )
                if (
                    "signal_consensus_convergence" in tool_names
                    and "signal_consensus_convergence" not in executed_tools
                ):
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall(
                                "call_arb_conv",
                                "signal_consensus_convergence",
                                {
                                    "summary_of_agreement": "VERDICT A: Consensus hardened and defended with challenger mitigations.",
                                    "dissenting_points": "Minor operational complexity accepted.",
                                },
                            )
                        ],
                        model=model,
                    )

            # Control plane clarification request
            if needs_clarification and "request_user_clarification" not in executed_tools:
                if "load_skill" in tool_names and "control_plane" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_cp_clarify", "load_skill", {"skill_name": "control_plane"})
                        ],
                        model=model,
                    )
                if "request_user_clarification" in tool_names:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall(
                                "call_clarify",
                                "request_user_clarification",
                                {"question": "Which target region and RPO/RTO SLA should we design for?"},
                            )
                        ],
                        model=model,
                    )

            # Control plane consensus convergence signal
            if (
                needs_convergence
                and not is_challenger
                and not is_arbitrator
                and "signal_consensus_convergence" not in executed_tools
            ):
                if "load_skill" in tool_names and "control_plane" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_cp_conv", "load_skill", {"skill_name": "control_plane"})
                        ],
                        model=model,
                    )
                if "signal_consensus_convergence" in tool_names:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall(
                                "call_conv",
                                "signal_consensus_convergence",
                                {
                                    "summary_of_agreement": "Consensus reached on phased asynchronous event architecture.",
                                    "dissenting_points": "Operational monitoring overhead.",
                                },
                            )
                        ],
                        model=model,
                    )

            # External MCP tool execution
            if needs_mcp:
                mcp_tool_names = [n for n in tool_names if n and n.startswith("mcp__")]
                if not mcp_tool_names and "load_skill" in tool_names and "mcp_mock_db" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_load_mcp", "load_skill", {"skill_name": "mcp_mock_db"})
                        ],
                        model=model,
                    )
                if mcp_tool_names:
                    preferred = None
                    if "drop" in prompt_lower or "delete" in prompt_lower or "destructive" in prompt_lower:
                        preferred = next((n for n in mcp_tool_names if "drop" in n or "delete" in n), mcp_tool_names[0])
                    else:
                        preferred = next((n for n in mcp_tool_names if "query" in n or "read" in n), mcp_tool_names[0])
                    if preferred not in executed_tools:
                        table_name = "tmp_staging_orders" if "tmp_staging_orders" in prompt_lower else "orders"
                        return MockModelResponse(
                            content=None,
                            tool_calls=[
                                MockToolCall(
                                    "call_mcp_tool",
                                    preferred,
                                    {"table": table_name, "actor": "alice@acme.com"},
                                )
                            ],
                            model=model,
                        )

            # Unauthorized skill access test
            if needs_unauthorized_tool and "load_skill" in tool_names and "load_skill" not in executed_tools:
                return MockModelResponse(
                    content=None,
                    tool_calls=[
                        MockToolCall("call_unauth", "load_skill", {"skill_name": "document_authoring"})
                    ],
                    model=model,
                )

            # File read (Step A -> Step B)
            if needs_file_read:
                if "load_skill" in tool_names and "document_reader" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_load_doc_reader", "load_skill", {"skill_name": "document_reader"})
                        ],
                        model=model,
                    )
                if "read_document" in tool_names and "read_document" not in executed_tools:
                    target_doc = (
                        "compliance_guidelines.txt"
                        if "compliance_guidelines" in prompt_lower
                        else "sys_spec.txt"
                    )
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_read_doc", "read_document", {"rel_path": target_doc})
                        ],
                        model=model,
                    )

            # Web research (Step A -> Step B)
            if needs_web_search:
                if "load_skill" in tool_names and "web_research" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_load_web", "load_skill", {"skill_name": "web_research"})
                        ],
                        model=model,
                    )
                if "web_search" in tool_names and "web_search" not in executed_tools:
                    q = (
                        "vector database pricing"
                        if "vector" in prompt_lower
                        else "cloud compute pricing comparison 2025"
                    )
                    return MockModelResponse(
                        content=None,
                        tool_calls=[MockToolCall("call_web_search", "web_search", {"query": q})],
                        model=model,
                    )

            # Document authoring (Step A -> Step B)
            if needs_doc_authoring:
                if "load_skill" in tool_names and "document_authoring" not in loaded_skills:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall("call_load_doc_author", "load_skill", {"skill_name": "document_authoring"})
                        ],
                        model=model,
                    )
                if "write_document" in tool_names and "write_document" not in executed_tools:
                    return MockModelResponse(
                        content=None,
                        tool_calls=[
                            MockToolCall(
                                "call_write_doc",
                                "write_document",
                                {
                                    "rel_path": "llm_cost_reduction_plan.md",
                                    "content": (
                                        "# LLM Cost Reduction Plan\n\n"
                                        "1. Cache prompt embeddings\n"
                                        "2. Route classification to fast-tier models\n"
                                        "3. Enforce thread expense circuit breakers."
                                    ),
                                },
                            )
                        ],
                        model=model,
                    )

        # 3. Final Discussion Response
        is_croatian = (
            "croatian" in sys_lower
            or "hrvatskom" in sys_lower
            or any(
                "strictly in croatian" in str(m.get("content", "")).lower()
                for m in messages
            )
            or any("hrvatskom" in str(m.get("content", "")).lower() for m in messages)
        )

        max_words_match = re.search(
            r"(?:MAXIMUM WORD COUNT|strictly NOT exceed|max_words).*?(\d+)",
            sys_text_all,
            re.IGNORECASE,
        )
        max_limit = int(max_words_match.group(1)) if max_words_match else None

        min_words_match = re.search(
            r"(?:MINIMUM WORD COUNT|minimum length of|min_words).*?(\d+)",
            sys_text_all,
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

        warrants_nomination = (
            "/first" in prompt_lower
            or "/open_debate" in prompt_lower
            or "/open-debate" in prompt_lower
            or "nominate" in prompt_lower
            or "[innovator]" in prompt_lower
            or "who speaks next" in prompt_lower
        )

        nomination_tag = ""
        if warrants_nomination:
            if "sceptic" in prompt_lower:
                nomination_tag = " /[LegalAdvisor]"
            else:
                nomination_tag = " /[Sceptic]"

        if is_challenger:
            base_text = (
                "Executive Challenge Thesis: The emerging consensus overlooks backpressure saturation and schema drift under peak failover.\n"
                "- Unstated Premise: Assumes consumers recover idempotently without poison-pill quarantine.\n"
                "- Mitigation: Enforce dead-letter quotas and contract validation.\n"
                "/[Arbitrator] next"
            )
        elif is_arbitrator:
            base_text = (
                "VERDICT A: Consensus Hardened & Converged.\n"
                "- Validated Defenses: Asynchronous decoupling preserves fault isolation.\n"
                "- Adopted Mitigations: Added dead-letter quarantine and schema registry validation."
            )
        elif is_croatian:
            base_text = (
                f"Kao tehnički stručnjak, smatram da je modularni pristup temelj stabilnosti našeg sustava. "
                f"Potrebno je osigurati odgovarajuću izolaciju servisa, definirati ugovore i pratiti latenciju.{nomination_tag}"
            )
        elif (
            "Permission denied" in joined_tool_results
            or "not authorized" in joined_tool_results
        ) and has_tool_results:
            base_text = (
                f"I attempted to execute the action, but received an authorization error: {joined_tool_results[:200]}. "
                f"I am not authorized to load the requested skill in this thread.{nomination_tag}"
            )
        elif needs_clarification and "request_user_clarification" in executed_tools:
            base_text = (
                "Before finalizing this architecture, I need clarification from the user: "
                "Which target region and RPO/RTO SLA should we design for?"
            )
        elif needs_mcp and has_tool_results:
            base_text = (
                f"External MCP tool executed successfully and returned: {joined_tool_results[:250]}.{nomination_tag}"
            )
        elif needs_file_read and has_tool_results:
            if "compliance_guidelines" in all_user_lower:
                base_text = (
                    f"According to 'agents_resources/compliance_guidelines.txt', retention obligations require 7 years "
                    f"for audit logs and encrypted archival storage. Details: {joined_tool_results[:180]}{nomination_tag}"
                )
            else:
                base_text = (
                    f"According to 'agents_resources/sys_spec.txt', the system throughput is specified at 5000 rps "
                    f"with latency target <50ms under peak load.{nomination_tag}"
                )
        elif needs_web_search and has_tool_results:
            if "vector" in prompt_lower:
                base_text = (
                    f"Based on our web search analysis for vector databases:\n"
                    f"• Pinecone provides a fully-managed cloud vector indexing service.\n"
                    f"• Qdrant offers high-performance Rust-based vector search with payload filtering.\n"
                    f"• Milvus is optimized for massive billion-scale vector operations.{nomination_tag}"
                )
            else:
                base_text = (
                    f"Based on current official pricing from Anthropic and OpenAI:\n"
                    f"• Claude 3.7 Sonnet: $3.00 / MTok input, $15.00 / MTok output (Source: https://anthropic.com/pricing)\n"
                    f"• GPT-4o: $2.50 / MTok input, $10.00 / MTok output (Source: https://openai.com/api/pricing){nomination_tag}"
                )
        elif needs_doc_authoring and has_tool_results:
            base_text = (
                f"I have drafted the 3-point business case and successfully saved it to 'agents_resources/llm_cost_reduction_plan.md'. "
                f"The document details prompt caching, tier optimization, and governance thresholds.{nomination_tag}"
            )
        elif "pii_echo_test" in prompt_lower:
            # Echoes masked PII token + unmasked ORG back so execute_llm_with_retry unmasks personal PII
            tokens = re.findall(r"\[(?:EMAIL_ADDRESS|PERSON|PHONE_NUMBER|PER)_\d+\]", user_prompt_str)
            tok_str = " ".join(tokens) if tokens else "the contact"
            base_text = f"Confirmed stakeholder {tok_str} at Acme Corp for onboarding.{nomination_tag}"
        else:
            base_text = (
                f"From an architectural standpoint, decoupling our pipeline into asynchronous event streams provides fault tolerance "
                f"and elastic horizontal scalability under peak load.{nomination_tag}"
            )

        if min_limit and min_limit >= 10:
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


async def mock_execute_local_tool(tool_name: str, arguments: dict[str, Any] | str) -> str:
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

    from services.local_tools import execute_local_tool

    return await execute_local_tool(tool_name, arguments)
