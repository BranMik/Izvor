# Live AI Evaluation & Rating Suite

Izvor's **Live AI Evaluation & Rating Suite** (`tests/evals/`) complements deterministic unit/integration tests and the binary E2E harness ([`tests/e2e/`](../../tests/e2e)) by measuring **graded output quality, statistical accuracy, retrieval fidelity, and cost/latency trends** against real LLMs and real vector embeddings.

---

## 1. Evaluation Philosophy & Guardrails

- **Live-Only Execution (`tests/evals/`):** Every suite in `tests/evals/` calls real LLM providers and embedding endpoints configured in `.env`. There is no `--mock` mode inside `tests/evals/` (deterministic mocked coverage of the eval framework itself lives in [`tests/unit/test_evals_framework.py`](../../tests/unit/test_evals_framework.py)).
- **Excluded from Default `pytest`:** `pyproject.toml` lists `"tests/evals"` in `norecursedirs`, so running `pytest` stays fast, offline, and free.
- **Full State & Path Isolation:** [`eval_isolation()`](../../tests/evals/isolation.py) redirects SQLite (`eval_izvor.db`), agent/system/app settings, workflows, pricing cache, audit logs, flat histories, thread notes, `local_tools` (`agents_resources/`), and LanceDB into a temporary directory so live evals never mutate production files.
- **Hard Spend Circuit Breaker:** A shared `SpendTracker` checks `--max-cost-usd` (default `$1.00`) before every case and halts cleanly (`budget_halted=true`) if reached.

---

## 2. The 4 Evaluation Suites

| Suite | Dataset | Live Subsystems Exercised | Primary Metrics & Ratings |
|---|---|---|---|
| `debate` | [`debate.jsonl`](../../tests/evals/datasets/debate.jsonl) (5 cases) | `SlackProtocolClient` → FastAPI ingress → `DiscussionLoop` → `TurnPipeline` across `sandwich`, `scatter_gather`, `fixed`, document-grounded reading, and `Challenger` → `Arbitrator` stress-test gate | Deterministic structural checks + **1–5 LLM-as-judge rubric** (`overall_score`, `rating_5`, `fatal_flaws`) |
| `revisor` | [`revisor.jsonl`](../../tests/evals/datasets/revisor.jsonl) (22 cases: 12 EN + 10 HR; 11 faithful, 11 hallucinated) | `run_internal_revisor` two-stage verification over seeded `ThreadStateManager` prompts, round evidence, and cited peer statements | **Precision, Recall, F1, FPR, FNR, Accuracy** |
| `rag` | [`rag.jsonl`](../../tests/evals/datasets/rag.jsonl) (10 cases) | `SyncReconciliationEngine` + `EmbeddingService` + `VectorStore` + `RAGService` over the 3 canonical committed docs in [`config/rag_knowledge/`](../../config/rag_knowledge) | **Recall@K, Hit@K, MRR, Scope Isolation Rate** (`agent-10008` private doc isolation) |
| `guardrail` | [`guardrail.jsonl`](../../tests/evals/datasets/guardrail.jsonl) (20 uncontaminated holdout cases: 10 benign, 10 malicious) | `GuardrailService.is_prompt_injection()` and `screen_untrusted_tool_output()` using an isolated copy of `guardrail_signatures.lance` when seeded | **Precision, Recall, F1, FPR, FNR, Accuracy** + heuristic vs. semantic counts |

---

## 3. Rubric Dimensions & 1–5 Rating Scale

For `debate` scenarios, [`RubricJudge`](../../tests/evals/scorers.py) evaluates transcripts at `temperature=0.0` using a structured JSON schema:
- **1:** Broken, empty, off-topic, or hallucinated.
- **2:** Weak or incomplete; misses key constraints or persona roles.
- **3:** Acceptable baseline; addresses the prompt with minor gaps.
- **4:** Strong, well-structured dialectical reasoning and synthesis.
- **5:** Exceptional rigor, distinct non-anchored perspectives, grounded accuracy, and actionable synthesis.

`overall_score` is normalized to `0.0–1.0` (`mean(dimensions) / 5.0`), and `rating_5` scales back to `1.0–5.0`. A `debate` case passes when `overall_score >= 0.65`, `fatal_flaws` is empty, and deterministic structural checks (`min_messages_met`) pass.

### Provider-Agnostic Judge Resolution
Judge model resolution follows this precedence:
1. `--judge-model <provider/model>` CLI flag
2. `IZVOR_EVAL_JUDGE_MODEL` environment variable
3. `default_reasoning_model` from [`config/settings/system_settings.json`](../../config/settings/system_settings.json)

If the selected model's API key is missing in `.env`, `resolve_model_with_fallback()` automatically falls back to an active provider key.

---

## 4. Running Evals & Baseline Regression Tracking

### CLI Commands
```bash
# Show all CLI flags
cmd /c .venv\Scripts\python.exe -u -m tests.evals.runner --help

# Run all 4 suites (capped at $1.00) and save a named baseline
cmd /c .venv\Scripts\python.exe -u -m tests.evals.runner --suite all --max-cost-usd 1.00 --save-baseline baseline

# Run a single suite or specific case IDs (low-cost smoke check)
cmd /c .venv\Scripts\python.exe -u -m tests.evals.runner --suite rag,guardrail --case RAG-01,GRD-01 --max-cost-usd 0.25

# Compare current branch against saved baseline and fail CI/local gate on >5% score drop
cmd /c .venv\Scripts\python.exe -u -m tests.evals.runner --suite all --compare-baseline baseline --regression-tolerance 0.05 --fail-on-regression
```

### Pytest Execution
```bash
# Run all eval suites via pytest
cmd /c .venv\Scripts\python.exe -u -m pytest tests/evals/test_evals_suite.py -v

# Run targeted cases with custom spend cap
cmd /c .venv\Scripts\python.exe -u -m pytest tests/evals/test_evals_suite.py -v --eval-case RAG-01,GRD-01 --eval-max-cost-usd 0.25
```

### Reports (`tests/evals/results/`)
Each run writes timestamped JSON and Markdown reports (`eval_<run_id>.json`, `eval_<run_id>.md`) and optional baseline snapshots (`baseline.json`) to `tests/evals/results/` (gitignored).

---

## 5. Extending Golden Datasets

Add new JSONL records to the appropriate file under `tests/evals/datasets/`, keeping balanced positive/negative labels for classification suites (`revisor.jsonl`, `guardrail.jsonl`) and referencing only committed knowledge files in `rag.jsonl`. Then run the offline dataset schema gate:

```bash
cmd /c .venv\Scripts\python.exe -u -m pytest tests/unit/test_evals_framework.py -v
```

