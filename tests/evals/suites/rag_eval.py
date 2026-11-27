from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from services.rag.document_parser import DocumentParser
from services.rag.embeddings import EmbeddingService
from services.rag.manifest_manager import ManifestManager
from services.rag.service import RAGService
from services.rag.sync_engine import SyncReconciliationEngine
from services.rag.vector_store import VectorStore
from tests.evals.isolation import PROJECT_ROOT, eval_isolation
from tests.evals.models import EvalCaseResult, SpendTracker, SuiteSummary
from tests.evals.scorers import compute_retrieval_metrics

DEFAULT_RAG_DATASET = PROJECT_ROOT / "tests" / "evals" / "datasets" / "rag.jsonl"
CANONICAL_RAG_FILES = (
    "shared/system_overview.md",
    "shared/rag_instructions.md",
    "agent-10008/security_policy.md",
)


def _load_cases(dataset_path: Path, case_ids: set[str] | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for raw_line in dataset_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if case_ids and str(item.get("id", "")).upper() not in case_ids:
            continue
        cases.append(item)
    return cases


def _stage_canonical_knowledge(dest_dir: Path) -> None:
    source_root = PROJECT_ROOT / "config" / "rag_knowledge"
    for rel_path in CANONICAL_RAG_FILES:
        src = source_root / rel_path
        if not src.is_file():
            raise FileNotFoundError(f"Canonical RAG knowledge document missing: {src}")
        target = dest_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _read_embedding_spend() -> tuple[int, float]:
    try:
        from pipeline.spend_governance import spend_governance

        metrics = spend_governance.calculate_spend("system:embeddings")
        return int(metrics.total_prompt_tokens), float(metrics.total_cost_usd)
    except Exception:
        return 0, 0.0


async def run_rag_suite(
    *,
    spend_tracker: SpendTracker,
    trials: int = 1,
    case_ids: set[str] | None = None,
    dataset_path: Path = DEFAULT_RAG_DATASET,
) -> SuiteSummary:
    """Evaluates live SyncReconciliationEngine + VectorStore + RAGService retrieval and agent isolation."""
    cases = _load_cases(dataset_path, case_ids=case_ids)
    results: list[EvalCaseResult] = []
    suite_start = time.time()
    suite_cost = 0.0

    for trial_num in range(1, max(1, int(trials)) + 1):
        spend_tracker.check_budget()
        with eval_isolation() as iso:
            temp_kb = iso.root / "rag_knowledge"
            _stage_canonical_knowledge(temp_kb)

            manifest_mgr = ManifestManager(manifest_path=iso.root / "file_manifest.json")
            store = VectorStore(db_dir=iso.lancedb_dir)
            embedder = EmbeddingService()
            parser = DocumentParser()
            sync_engine = SyncReconciliationEngine(
                knowledge_dir=temp_kb,
                manifest_manager=manifest_mgr,
                store=store,
                embedder=embedder,
                parser=parser,
            )
            rag_svc = RAGService(store=store, embedder=embedder)

            before_toks, before_usd = _read_embedding_spend()
            sync_stats = await sync_engine.reconcile()
            after_toks, after_usd = _read_embedding_spend()
            sync_cost = max(0.0, after_usd - before_usd)
            sync_toks = max(0, after_toks - before_toks)
            if sync_cost > 0 or sync_toks > 0:
                spend_tracker.record_usd(
                    sync_cost,
                    model=embedder.model,
                    prompt_tokens=sync_toks,
                )
                suite_cost += sync_cost

            if sync_stats.get("errors"):
                raise RuntimeError(f"RAG reconciliation failed: {sync_stats['errors']}")

            for idx, case in enumerate(cases):
                spend_tracker.check_budget()
                case_id = str(case["id"])
                title = str(case.get("title", case_id))
                query = str(case["query"])
                agent_name = str(case["agent_name"])
                scope = str(case.get("scope", "all"))
                top_k = int(case.get("top_k", 3))
                expected_doc_ids = list(case.get("expected_doc_ids") or [])
                forbidden_doc_ids = list(case.get("forbidden_doc_ids") or [])

                case_start = time.time()
                case_toks = sync_toks if idx == 0 else 0
                case_cost = sync_cost if idx == 0 else 0.0
                error_msg: str | None = None
                retrieved_doc_ids: list[str] = []
                metrics: dict[str, Any] = {}

                try:
                    q_before_toks, q_before_usd = _read_embedding_spend()
                    vectors = await embedder.get_embeddings([query])
                    q_vec = vectors[0] if vectors else None
                    raw_hits = await asyncio.to_thread(
                        store.search,
                        query_text=query,
                        query_vector=q_vec,
                        agent_name=agent_name,
                        scope=scope,
                        top_k=top_k,
                    )
                    # Sanity check formatted RAGService path for the first case without duplicate embedding cost
                    if idx == 0:
                        await asyncio.to_thread(rag_svc.vector_store.get_or_create_table)
                    q_after_toks, q_after_usd = _read_embedding_spend()

                    delta_usd = max(0.0, q_after_usd - q_before_usd)
                    delta_toks = max(0, q_after_toks - q_before_toks)
                    if delta_usd > 0 or delta_toks > 0:
                        spend_tracker.record_usd(
                            delta_usd,
                            model=embedder.model,
                            prompt_tokens=delta_toks,
                        )
                        suite_cost += delta_usd
                        case_toks += delta_toks
                        case_cost += delta_usd

                    retrieved_doc_ids = [
                        str(hit.get("doc_id", "")).replace("\\", "/")
                        for hit in raw_hits
                        if hit.get("doc_id")
                    ]
                    metrics = compute_retrieval_metrics(
                        retrieved_doc_ids=retrieved_doc_ids,
                        expected_doc_ids=expected_doc_ids,
                        forbidden_doc_ids=forbidden_doc_ids,
                        k=top_k,
                    )
                except Exception as exc:
                    error_msg = str(exc)
                    metrics = {
                        "recall_at_k": 0.0,
                        "hit_at_k": 0.0,
                        "mrr": 0.0,
                        "scope_isolation_passed": False,
                        "score": 0.0,
                    }

                score = float(metrics.get("score", 0.0))
                isolation_ok = bool(metrics.get("scope_isolation_passed", False))
                passed = (error_msg is None) and isolation_ok and (score >= 0.6)

                results.append(
                    EvalCaseResult(
                        case_id=case_id,
                        suite="rag",
                        title=title,
                        trial=trial_num,
                        score=round(score, 4),
                        rating_5=round(score * 5.0, 2),
                        passed=passed,
                        metrics={
                            **metrics,
                            "retrieved_doc_ids": retrieved_doc_ids,
                            "expected_doc_ids": expected_doc_ids,
                            "forbidden_doc_ids": forbidden_doc_ids,
                        },
                        deterministic_checks={"scope_isolation_passed": isolation_ok},
                        details=(
                            error_msg
                            if error_msg
                            else f"retrieved={retrieved_doc_ids} isolation={isolation_ok}"
                        ),
                        duration_seconds=round(time.time() - case_start, 3),
                        prompt_tokens=case_toks,
                        completion_tokens=0,
                        cost_usd=round(case_cost, 6),
                        error=error_msg,
                    )
                )

    n = len(results)
    mean_score = sum(r.score for r in results) / n if n > 0 else 0.0
    pos_cases = [r for r in results if r.metrics.get("expected_doc_ids")]
    agg = {
        "mean_recall_at_k": round(
            sum(float(r.metrics.get("recall_at_k", 0.0)) for r in pos_cases) / len(pos_cases), 4
        )
        if pos_cases
        else 1.0,
        "mean_hit_at_k": round(
            sum(float(r.metrics.get("hit_at_k", 0.0)) for r in pos_cases) / len(pos_cases), 4
        )
        if pos_cases
        else 1.0,
        "mean_mrr": round(
            sum(float(r.metrics.get("mrr", 0.0)) for r in pos_cases) / len(pos_cases), 4
        )
        if pos_cases
        else 1.0,
        "scope_isolation_rate": round(
            sum(1 for r in results if r.metrics.get("scope_isolation_passed")) / n, 4
        )
        if n > 0
        else 0.0,
    }

    return SuiteSummary(
        suite="rag",
        total_cases=len(cases),
        trials_per_case=max(1, int(trials)),
        mean_score=round(mean_score, 4),
        mean_rating_5=round(mean_score * 5.0, 2),
        aggregate_metrics=agg,
        total_cost_usd=round(suite_cost, 6),
        total_duration_seconds=round(time.time() - suite_start, 3),
        cases=results,
    )

