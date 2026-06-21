# Coding Agent Latency/Token Benchmark

This benchmark uses 12 lightweight cases derived from `SWE-bench/SWE-bench_Verified`.
It is intended to evaluate ForgeCC agent-loop latency and token usage, not full
SWE-bench patch-solving performance.

## Source Fields

Cases are sampled from these SWE-bench Verified fields:

- `repo`, `instance_id`, `base_commit`, `environment_setup_commit`, `version`
- `problem_statement`, `hints_text`
- `patch`, `test_patch`
- `FAIL_TO_PASS`, `PASS_TO_PASS`
- `difficulty`

Derived metrics:

- `patch_file_count`: unique files in the gold implementation patch
- `test_file_count`: unique files in the test patch
- `fail_to_pass_count`: number of failing tests introduced by the test patch
- `pass_to_pass_count`: number of regression tests
- text lengths for `problem_statement`, `hints_text`, `patch`, and `test_patch`

## Sampling Criteria

The 12-case latency/token set favors ordinary, bounded tasks:

- Prefer `<15 min fix`; allow a small number of `15 min - 1 hour` cases for variety.
- Prefer `patch_file_count <= 2` and `test_file_count <= 2`.
- Prefer `fail_to_pass_count <= 3`.
- Prefer concise issue statements so model reasoning latency does not dominate.
- Cover all 12 repositories in SWE-bench Verified when possible.
- Avoid `1-4 hours` and `>4 hours` cases; reserve those for context-compression benchmarks.

## Task Modes

- `localize_from_issue`: issue-only localization; measures search/read sequencing.
- `parallel_read_gold_files`: gold implementation/test file paths are provided; measures
  independent read batching and tool-call latency.
- `test_plan_from_failures`: FAIL_TO_PASS tests are provided; measures targeted diagnosis
  and token use without running a full suite.

## Runner Metrics

- `end_to_end_ms`
- `time_to_first_tool_ms`
- `prompt_tokens`
- `completion_tokens`
- `cached_prompt_tokens`

Per-tool execution timing requires a structured trace hook in ForgeCC and is
not recorded by the lightweight runner yet.

## Variants

ForgeCC always uses streaming model responses/tool-call deltas in this
benchmark. `run_benchmark.py` uses environment switches to run the remaining
ablations:

- `base`: streaming, serial safe tools, all tool schemas inline, no prompt cache
- `concurrent`: base plus concurrency-safe read/search/web tools
- `deferred`: streaming plus concurrency plus deferred tool schemas, no prompt cache
- `deferred_cache`: streaming plus concurrency plus deferred tool schemas plus prompt cache

The switches are:

- `CCA_DISABLE_TOOL_CONCURRENCY=1`
- `CCA_DISABLE_DEFERRED_TOOLS=1`
- `CCA_DISABLE_PROMPT_CACHE=1`

## Running

Prepare local repository checkouts under a root directory using `owner__repo`
names, for example:

```text
/tmp/swe-repos/
  django__django/
  sympy__sympy/
  pytest-dev__pytest/
```

Then run one or more variants:

```bash
python benchmarks/coding_agent/run_benchmark.py \
  --repos-root /tmp/swe-repos \
  --variant base \
  --variant concurrent \
  --variant deferred_cache
```

For a smoke test on one case:

```bash
python benchmarks/coding_agent/run_benchmark.py \
  --repos-root /tmp/swe-repos \
  --variant base \
  --case-id ca_swe_verified_001
```

Results are appended to `benchmarks/coding_agent/results/results.jsonl`.
