# Context/Memory Long-Task Benchmark

This benchmark uses long SWE-bench Verified tasks from the first 8 repositories
already used by the coding-agent benchmark:

- `django/django`
- `sympy/sympy`
- `pydata/xarray`
- `pytest-dev/pytest`
- `astropy/astropy`
- `sphinx-doc/sphinx`
- `matplotlib/matplotlib`
- `scikit-learn/scikit-learn`

The cases are designed to evaluate context compression and task-completion
behavior, not raw first-token latency.

## Sampling Criteria

Cases are selected from `SWE-bench/SWE-bench_Verified` using these fields:

- `difficulty`: prefer `1-4 hours`, with one complex `15 min - 1 hour` case
- `problem_statement`: prefer longer issue statements
- `patch` and `test_patch`: prefer long patches or multi-file changes
- `FAIL_TO_PASS`: include targeted failing tests in the prompt
- `PASS_TO_PASS`: prefer many regression tests as a proxy for broader context
- derived implementation/test file counts from `patch` and `test_patch`

## User Request Shape

Each prompt asks the agent to:

1. Diagnose the SWE-bench issue.
2. Build a compact map of relevant source files and tests before editing.
3. Implement a minimal fix.
4. Run targeted `FAIL_TO_PASS` tests and a small relevant `PASS_TO_PASS` subset when practical.
5. End with a compact summary of changed files, tests, and residual risk.

## Suggested Metrics

- prompt token growth across turns
- compact/autocompact count
- large tool result persistence count
- gold implementation-file recall
- targeted test completion
- final task completion
- whether the final summary preserves key context after compression

## Running

Prepare separate `owner__repo` local checkouts for each variant if you want to
avoid one run's edits affecting the other, then run each variant against its
own repo root:

```bash
python benchmarks/context_memory/run_context_benchmark.py \
  --repos-root /tmp/swe-repos-no-compression \
  --variant no_compression

python benchmarks/context_memory/run_context_benchmark.py \
  --repos-root /tmp/swe-repos-compression \
  --variant compression
```

For a smoke test on one case:

```bash
python benchmarks/context_memory/run_context_benchmark.py \
  --repos-root /tmp/swe-repos \
  --variant compression \
  --case-id ctx_swe_verified_front8_001 \
  --max-turns 8
```

Variants:

- `no_compression`: disables ForgeCC context compression while keeping prompt cache enabled when supported.
- `compression`: enables context compression while keeping prompt cache enabled when supported.

By default, the runner does not set `CCA_EFFECTIVE_CONTEXT_WINDOW`; it uses the
model/backend default context behavior. Pass `--effective-window` only when you
want to force a smaller effective window for a specific experiment.

Results are appended to `benchmarks/context_memory/results/results.jsonl`.

While running, the benchmark prints live events for case start/finish, tool
calls, token snapshots, context compaction, large-result persistence, and
timeouts. Pass `--quiet-events` to suppress these live events and keep only the
final per-case summary line.
