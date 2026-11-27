from __future__ import annotations

from tests.evals.suites.debate_eval import run_debate_suite
from tests.evals.suites.guardrail_eval import run_guardrail_suite
from tests.evals.suites.rag_eval import run_rag_suite
from tests.evals.suites.revisor_eval import run_revisor_suite

ALL_SUITES: tuple[str, ...] = ("debate", "revisor", "rag", "guardrail")

SUITE_RUNNERS = {
    "debate": run_debate_suite,
    "revisor": run_revisor_suite,
    "rag": run_rag_suite,
    "guardrail": run_guardrail_suite,
}

__all__ = [
    "ALL_SUITES",
    "SUITE_RUNNERS",
    "run_debate_suite",
    "run_guardrail_suite",
    "run_rag_suite",
    "run_revisor_suite",
]

