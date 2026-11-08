# Fault Localization Feature

This directory contains the fault-localization portion of the bug-tracking LLM
project. It is a retrieval-first implementation that indexes repository source
code, ranks suspicious files and symbols against a bug report, and optionally
reranks the bounded candidate set with a local Ollama model.

The trainable target architecture, data contract, leakage rules, and file- and
symbol-level evaluation definitions are specified in
[`FAULT_LOCALIZATION_MODEL_SPEC.md`](FAULT_LOCALIZATION_MODEL_SPEC.md). Read and
update that specification before implementing or changing model training.
The current full SWE-bench 2,294-ticket experiment is documented in
[`reports/fault_localization/SWEBENCH_FULL_STAGE1_EXPERIMENT_REPORT_ZH.md`](reports/fault_localization/SWEBENCH_FULL_STAGE1_EXPERIMENT_REPORT_ZH.md).
The earlier SWE-bench Lite 300-ticket Stage-1 v11 report remains available as
historical evidence in
[`reports/fault_localization/STAGE1_V11_FINAL_REPORT_ZH.md`](reports/fault_localization/STAGE1_V11_FINAL_REPORT_ZH.md).

It is not a copy of the complete bug-tracking pipeline. Commands and paths in
this README only refer to files that exist in this directory.

## Implemented flow

```text
bug ticket
  -> input-quality checks
  -> repository code index
  -> TF-IDF, optional full SBERT, or bounded TF-IDF -> SBERT rerank
  -> stack trace / component / keyword / symbol scoring
  -> optional high-precision domain/path routing
  -> one best chunk per file with supporting chunks
  -> explicit Top-20 unique Stage-1 candidate files
  -> optional validated Ollama rerank blended with retrieval
  -> confidence and patch-handoff gate
  -> JSON result and Top-k/MRR evaluation
```

Supported source suffixes include Python, JavaScript/TypeScript, Java, C/C++,
Go, and Rust. Python symbols are extracted with `ast`; other languages use a
lightweight declaration and block-range parser.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Build a reusable code index

```bash
python3 scripts/build_code_index.py \
  --repo-path path/to/repository \
  --output data/code_index/project.json
```

Tests are excluded by default. Use `--include-tests` only when test files are
valid localization targets.

The index records per-file SHA-256 fingerprints. A later incremental build can
reuse chunks for unchanged files.

## Localize one ticket

With a prebuilt index:

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --code-index data/code_index/project.json \
  --output prediction.json \
  --top-k 5
```

Or build the index from a repository:

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --repo-path path/to/repository
```

The default output is file-aggregated: each `localized_candidates` entry points
to a different file, while `supporting_chunks` retains other matching symbols
from that file. Use `--no-file-aggregation` for controlled chunk-level
experiments.

## Batch mode, cache, progress, and resume

```bash
python3 scripts/fault_localization.py \
  --tickets-jsonl tickets.jsonl \
  --repo-path path/to/repository \
  --index-cache-dir data/code_index/cache \
  --output predictions.jsonl \
  --progress json \
  --checkpoint-every 25
```

If a run stops, repeat the command with `--resume`. Completed ticket IDs from
the existing output are retained and skipped. Checkpoints are written
atomically. When repository files change, the cached index rebuilds only the
changed source files and reuses unchanged chunks.

Use `--force-reindex` to ignore a current cache.

## Output contract

The existing compatibility fields remain available:

- `localized_candidates`: ranked primary code chunks.
- `bug_location`: best file, symbol, and line range.
- `candidates`: legacy candidate representation.

Additional safety and file-level fields include:

- `localized_files`: one entry per suspicious file.
- `input_validation`: sparse or invalid ticket diagnostics.
- `confidence_level`: `high`, `medium`, or `low`.
- `should_manual_review` and `recommend_patch_generation`.
- `patch_generation_policy`: patch suggestion, manual review, or block.

Only high-confidence results are recommended for patch suggestion. Medium and
low confidence results remain localization hints for human review.

## Stage-1 candidate retrieval contract

The reportable Stage-1 boundary is now explicit:

1. score repository code chunks with TF-IDF plus code-evidence signals;
2. add a bounded domain-to-source-path routing signal from production ticket text;
3. in hybrid mode, select one TF-IDF representative per file and rerank at most 50 unique files with SBERT;
4. aggregate chunks by file and preserve the best 20 files;
5. only then allow an optional LLM/File reranker to change the final Top-K.

`stage1_candidate_files` is therefore the pre-LLM Top-20 output used by
Candidate Hit@20 and Candidate Recall@20. `localized_files` remains the final
Top-K output. Every prediction also records `stage1_diagnostics`, including the
requested/returned candidate counts and the exact stage boundary.

The hybrid mode implements `Top 50 unique files -> Top 20 files`. It can
be stated explicitly with `--semantic-candidate-k 50 --candidate-file-k 20`.
Ticket IDs, benchmark hints, failing-test labels, and duplicate text fields are
not included in the retrieval query.

Domain-path routing is deliberately explainable: every activated rule is saved
in `matching_domain_intents`. Because these mappings are tuned on development
errors, improvements must be confirmed on untouched tickets and the frozen
holdout before being reported as generalization.

## Optional SBERT retrieval

Install `sentence-transformers`, then run:

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --repo-path path/to/repository \
  --embedding-backend sbert
```

`--embedding-backend auto` falls back to TF-IDF when a local SBERT model is not
available. `--allow-sbert-download` permits model download.

For large repositories, prefer the bounded hybrid mode. It applies the existing
TF-IDF and code-evidence ranking to all chunks, then sends only the best 50
chunks to SBERT:

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --repo-path path/to/repository \
  --embedding-backend tfidf-sbert-rerank \
  --semantic-candidate-k 50 \
  --candidate-file-k 20
```

## Optional Ollama rerank

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --repo-path path/to/repository \
  --llm-rerank \
  --llm-candidate-k 10 \
  --ollama-model codellama:7b-instruct \
  --ollama-timeout 180
```

The prompt is bounded, and the timeout is enforced as a hard request deadline.
The model is asked to return every candidate exactly once. Invalid and duplicate
rows are removed; incomplete coverage, an unavailable service, or non-finite
scores fall back to retrieval. Valid LLM scores are blended with retrieval at
30%/70% instead of replacing the calibrated score.
The LLM service is optional and is not required for the default TF-IDF flow.

### Integrated Stage 1 -> Stage 2 -> deterministic AST symbol flow

The formal Stage-3 B1 baseline does not require Ollama. It keeps the Stage-2
Top-5 files, emits a deterministic Top-30 symbol pool, and returns Top-5
file-qualified symbols:

```bash
python3 scripts/fault_localization.py \
  --ticket ticket.json \
  --repo-path path/to/repository \
  --candidate-file-k 20 \
  --top-k 5 \
  --symbol-localization \
  --symbol-retrieval-mode b1-structured \
  --symbol-per-file-quota 0 \
  --symbol-candidate-k 30 \
  --symbol-top-k 5 \
  --output prediction.json
```

Add `--symbol-llm-rerank --ollama-model codellama:7b-instruct` only for the
experimental Symbol LLM comparison. `--symbol-llm-rerank` automatically
enables deterministic symbol localization. The old `--symbol-rerank` option is
a deprecated compatibility alias that enables both modes.

The integrated output keeps each boundary separately:

- `stage1_candidate_files`: retrieval Top-20 before any LLM call.
- `stage2_localized_files`: file Top-5 after a valid LLM rerank, or retrieval fallback.
- `stage3_candidate_symbols`: deterministic, file-qualified symbol Top-30.
- `stage3_retrieval_symbols`: deterministic symbol Top-5 baseline.
- `stage3_ranked_symbols`: unique file-qualified AST symbols after a valid LLM rerank, or symbol-retrieval fallback.
- `stage3_diagnostics`: localization/LLM eligibility, attempted/valid state, candidate counts, timeout, and fallback reason.

Both rerankers require complete, finite, one-row-per-candidate model output.
Invalid or incomplete output is rejected instead of being reported as an LLM
result. AST symbols are read from the portable code index, so a loaded index
does not depend on its original machine-specific repository path.

The frozen development comparison can be reproduced without an LLM:

```bash
python3 scripts/run_stage3_deterministic_g2.py
```

It compares B0 TF-IDF, B1 structured evidence, and B1 per-file quotas 4/6/8
against the same saved Stage-2 Top-5. Stage-3 now exposes module-level gaps as
the exact identity `<module>` and synthesizes one module candidate for legacy
indexes that only contain a generic file chunk. The 61-ticket development run
now selects `coverage-aware-v1` over global B1 and B0 (68.64%, 64.36%, and
60.60% Conditional Exact Candidate Recall@30, respectively), but still does
not pass the 90% G2 gate. Symbol LLM experiments therefore remain blocked
until deterministic ranking improves.

Run the exact AST-pool ceiling analysis with:

```bash
python3 scripts/analyze_stage3_symbol_pool_oracle.py
```

After adding module candidates, the development pool oracle rises from 81.63%
to 90.41%. `coverage-aware-v1` preserves the strongest global prefix, reserves
one module candidate per Stage-2 file, and expands class families evidenced by
that prefix. It recovers 7 of the original 37 pool-reachable Top-30 misses:
exact Top-30 hits rise from 81 to 88 and 30 ranking misses remain. The frozen
source-neighborhood ablation is complete: Recall@30 was 64.70%, recovering one
miss but losing seven previous hits. Therefore coverage-aware-v1 remains the
selected baseline at 68.64%. Reproduce the separate experiment with
`python scripts/run_stage3_deterministic_g2.py --config configs/fault_localization/stage3_source_neighborhood_v1.json`;
its report and paired outcomes are in
`reports/fault_localization/stage3_source_neighborhood_dev_v1/`.

The frozen one-hop caller/callee ablation is also complete: Recall@30 was
66.02%, recovering two misses but losing six previous hits. Coverage-aware-v1
remains selected at 68.64%. Reproduce it with
`python scripts/run_stage3_deterministic_g2.py --config configs/fault_localization/stage3_call_neighborhood_v1.json`.
The protocol, limitations, and paired outcomes are in
`reports/fault_localization/stage3_call_neighborhood_dev_v1/EXPERIMENT_ZH.md`.
Reachability analysis of the selected baseline's 30 ranking misses finds only
3 one-hop links from the global prefix, 5 links exclusively from candidates
outside that prefix, and 22 symbols with no resolved incident edge in the
current static graph. Of the two call-variant recoveries, only one is explained
by a prefix call edge; the other comes from changed backfill. Run
`python scripts/analyze_stage3_call_reachability.py` for the saved per-symbol
evidence.

The 22 no-edge cases were then audited against base-commit source and the
cached repository-level graph. All base definitions are present. Twenty cases
already have a cached graph edge whose peer is also in the Stage-2 candidate
pool: 15 are same-file graph reconstruction losses and 5 are cross-file import
resolution losses. One case is excluded by Stage-2 pool scope and one is a
dynamic-dispatch case; none lacks all static-call evidence. The next frozen
variant should filter the cached graph by candidate identities instead of
rebuilding a graph from symbol-only chunks.

`call-neighborhood-v2` implements that correction by using the cached
repository-level graph and filtering both endpoints to the current candidate
pool. On the same development data it improves call-neighborhood-v1 from
66.02% to 68.20% Recall@30, but remains below coverage-aware-v1 at 68.64%.
Against that selected baseline it recovers four ranking misses and loses five
previous hits, so it is not adopted. The frozen protocol and paired outcomes
are saved in `reports/fault_localization/stage3_call_neighborhood_dev_v2/`.

For local Ollama models, reranking uses an explicit JSON Schema and batches at
most five candidates per request. Stage 2 still evaluates all 20 file
candidates and Stage 3 still evaluates the bounded symbol pool; batching only
reduces instruction-following pressure on smaller models. If any batch is
invalid, the whole stage falls back to its retrieval order.

## Prepare and evaluate ground truth

Normalize project-specific bug-fix records:

```bash
python3 scripts/prepare_fault_localization_gold.py \
  --input bug_fix_records.jsonl \
  --output gold.jsonl \
  --skip-empty
```

Evaluate predictions:

```bash
python3 scripts/evaluate_fault_localization.py \
  --gold gold.jsonl \
  --pred predictions.jsonl \
  --output metrics.json
```

Metrics include Stage-1 Candidate Hit@20, Candidate Recall@20, average candidate
count, outcome counts (full recall, partial recall, and miss), plus file- and
symbol-level Top-1, Top-3, Top-5, and MRR. Missing predictions remain in the
denominator instead of being silently discarded. Interpret Candidate Hit@20 as
the proportion of tickets whose Top 20 contains at least one correct file;
interpret Candidate Recall@20 as the average proportion of all correct files
recovered per ticket. Recall is the primary Stage-1 optimization metric.
The evaluator also records the ticket IDs in each full/partial/miss outcome for
reproducible error analysis.

## Frozen Stage-1 v11 result

This section records the earlier 300-ticket SWE-bench Lite experiment and must
not be presented as the current full-dataset result. The current protocol uses
2,294 tickets; its selected TF-IDF Top-50 + SBERT method achieves Frozen
Holdout Hit@20 93.00%, Recall@20 84.47%, Top-1 54.80%, and MRR@20 0.6623.

The selected method is TF-IDF plus validated domain/path routing. Generic
routing, repository proximity, advanced file aggregation, SBERT, and LLM
reranking are disabled in the frozen method. Use `--frozen-stage1-v11` to make
the runner reject any parameter drift.

- Development: Recall@20 93.33% (196/210), 95% CI [90.00%, 96.67%].
- One-time cross-project Holdout: Recall@20 84.44% (76/90), 95% CI
  [76.67%, 91.11%].
- Every evaluated ticket returned exactly 20 unique files; both runs had zero
  failures.

The Holdout is below the 90% research target and must not be reused for v11
tuning. See the final Chinese report and the sealed evaluation manifest under
`reports/fault_localization/` for the per-repository breakdown and next work.

The equivalent experiment entry point is:

```bash
python3 experiments/evaluate_bug_localization.py \
  --gold gold.jsonl \
  --pred predictions.jsonl
```

## SWE-bench Lite preparation

```bash
python3 scripts/prepare_swebench_lite_fault_localization.py \
  --split test \
  --output-dir data/fault_localization/swebench_lite
```

This prepares ticket and file-level gold JSONL from developer patches.
SWE-bench Lite does not provide symbol-level ground truth by default.

Run the fixed TF-IDF baseline:

```bash
python3 scripts/run_swebench_lite_fault_localization.py \
  --dataset-dir data/fault_localization/swebench_lite \
  --split test \
  --clone-missing \
  --output-dir reports/fault_localization/swebench_lite_tfidf_baseline \
  --checkpoint-every 10 \
  --progress json
```

Repeat an interrupted run with the same arguments plus `--resume`. The runner:

- resolves every ticket's `repo` and `base_commit`;
- creates an isolated snapshot instead of switching the user's source checkout;
- caches one code index per repository commit;
- writes predictions and failures atomically;
- records an exact method ID so incompatible runs cannot be mixed on resume;
- records how many requested LLM reranks were used or fell back to retrieval;
- evaluates only the selected ticket subset.

Run SBERT and LLM comparisons into separate output directories:

```bash
python3 scripts/run_swebench_lite_fault_localization.py \
  --dataset-dir data/fault_localization/swebench_lite \
  --embedding-backend tfidf-sbert-rerank \
  --semantic-candidate-k 50 \
  --candidate-file-k 20 \
  --output-dir reports/fault_localization/swebench_lite_tfidf_sbert \
  --resume

python3 scripts/run_swebench_lite_fault_localization.py \
  --dataset-dir data/fault_localization/swebench_lite \
  --llm-rerank \
  --llm-candidate-k 10 \
  --output-dir reports/fault_localization/swebench_lite_tfidf_llm \
  --resume
```

Compare their metrics against TF-IDF:

```bash
python3 scripts/compare_fault_localization_runs.py \
  --run tfidf=reports/fault_localization/swebench_lite_tfidf_baseline/test_metrics.json \
  --run sbert=reports/fault_localization/swebench_lite_tfidf_sbert/test_metrics.json \
  --run llm=reports/fault_localization/swebench_lite_tfidf_llm/test_metrics.json \
  --baseline tfidf \
  --output reports/fault_localization/swebench_lite_method_comparison.json
```

The comparison reports metric deltas and warns if methods were evaluated with
different file- or symbol-level denominators, or if an LLM run contains
retrieval fallbacks. A one-ticket smoke run verifies execution only; use the
same frozen ticket subset across methods before interpreting accuracy deltas.

## Main files

- `src/utils/fault_localization.py`: indexing, retrieval, aggregation, and confidence.
- `src/modules/bug_localizer.py`: reusable integration class with in-memory incremental indexing.
- `scripts/build_code_index.py`: standalone index builder.
- `scripts/fault_localization.py`: single and batch CLI.
- `scripts/evaluate_fault_localization.py`: Candidate Hit/Recall@20, Top-k, and MRR evaluation.
- `scripts/prepare_fault_localization_gold.py`: gold normalization.
- `scripts/prepare_swebench_lite_fault_localization.py`: public dataset preparation.
- `scripts/run_swebench_lite_fault_localization.py`: resumable per-commit benchmark runner.
- `scripts/create_swebench_full_fault_localization_split.py`: deterministic 2,294-ticket protocol split.
- `scripts/run_swebench_full_stage1_experiment.py`: repository-parallel full-dataset runner.
- `scripts/freeze_swebench_full_stage1_experiment.py`: method and artifact freeze before holdout.
- `scripts/analyze_swebench_full_stage1_results.py`: Top-20 metrics and paired bootstrap analysis.
- `scripts/compare_fault_localization_runs.py`: TF-IDF/SBERT/LLM metric comparison.
- `tests/test_fault_localization.py`: regression and CLI tests.

## Known limitations

- Non-Python symbol extraction remains heuristic rather than parser-based.
- Full-repository SBERT remains available for controlled small-repository runs,
  but is substantially slower than bounded hybrid reranking on large projects.
- Real Ollama execution requires a running, locally accessible service and model.
- The confidence gate is rule-based and should be calibrated on a frozen
  held-out benchmark before production automation.
- Reproducing the full SWE-bench experiment requires repository downloads,
  substantial index storage, and a deliberate long-running benchmark invocation.
# 整合 Patch Generation 入口

本 patch runtime 透過獨立定位 JSON/JSONL 消費外部 Stage-3 結果；操作、輸出與限制見下方「External frozen Fault Localization handoff」。`scripts/smoke_repair_pipeline.py` 需提供外部定位檔、ticket 與目標 repository，不會自行執行本 repo 的定位副本。
# External frozen Fault Localization handoff

The patch runtime consumes localization produced by the separate
`fault_localization_feature_files` project. It does **not** import or run this
repository's `src/utils/fault_localization.py` at patch-generation time.

## Single-ticket flow

Install this project and the external FL dependencies in the Python environment
you use for localization. Export through the external `BugLocalizer` API; this
pins its TF-IDF baseline defaults and writes the required repository/commit/hash
metadata into the handoff:

```bash
python -m pip install -e .

python scripts/export_fl_localization.py \
  --ticket ticket.json \
  --repo /path/to/target-repository \
  --system-src /path/to/fault_localization_feature_files/src \
  --output localization.json

patch-pipeline \
  --ticket ticket.json \
  --localization localization.json \
  --repo /path/to/target-repository \
  --output runs/example
```

For a controlled prompt experiment when the public issue explicitly states a
positional argument replacement (for example, "the third argument of
`rotation_matrix` should be `units.radian`, rather than `False`"), add
`--explicit-argument-edit` to `patch-pipeline`. This opt-in flag restates that
issue-provided edit in the prompt only when the named call appears in the
selected source. It does not change the external localization handoff or the
candidate validation gates. A generated patch still needs functional testing.

The patch CLI accepts one localization JSON object or a JSONL file. For JSONL,
the record must identify its ticket (`ticket_id`, `instance_id`, `bug_id`, or
`id`); if the file contains multiple records, exactly one must match the
ticket. `stage3_ranked_symbols` is passed directly to the generator.

Before generation, the pipeline checks the ticket and localization repository
identity, both `base_commit` values, repository `HEAD`, a clean checkout, and
the SHA-256 of every ranked source file. It also rejects handoffs that do not
declare the frozen Stage-1 contract: TF-IDF, candidate-file K=20, top K=5,
domain-path routing, basic file aggregation, and no LLM/supervised reranker.
Ground-truth patch fields are not part of the generator prompt.

## Batch flow

The batch runner takes tickets and external localization records as separate
JSONL inputs, matched by ticket identifier:

```bash
python scripts/run_stage4_batch.py \
  --tickets tickets.jsonl \
  --localizations localizations.jsonl \
  --output runs/batch-001 \
  --repo-cache-dir data/repositories \
  --explicit-argument-edit
```

`--explicit-argument-edit` is optional. It restates a positional-argument
replacement only when the public issue names the old/new values and the call is
present in the selected source; all candidate gates still apply.

Export one localization record per ticket and combine them as JSONL. Keep the
two repositories side-by-side or use paths/environment settings for your own
machine. No local source path or model checkpoint is hard-coded in the runtime.

## Offline handoff smoke test

The CLI handoff is covered by a fake-backend test. It supplies an external
Stage-3 JSONL result, checks that its ranked function reaches the generator, and
does not contact Ollama:

```bash
python -m unittest discover -s tests -p 'test_repair_pipeline.py' -v
```
