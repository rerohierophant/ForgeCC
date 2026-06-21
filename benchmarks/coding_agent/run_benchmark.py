"""Run ForgeCC coding-agent latency/token cases.

The runner expects local checkouts for the SWE-bench repositories. It does not
clone repos or build SWE-bench environments; this benchmark measures ForgeCC
agent-loop behavior on bounded read/search/planning tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_CASES = Path(__file__).resolve().parent / "cases" / "swe_verified_latency_token_12.jsonl"
DEFAULT_OUT = Path(__file__).resolve().parent / "results" / "results.jsonl"

VARIANT_ENVS = {
    "base": {
        "CCA_DISABLE_TOOL_CONCURRENCY": "1",
        "CCA_DISABLE_DEFERRED_TOOLS": "1",
        "CCA_DISABLE_PROMPT_CACHE": "1",
    },
    "concurrent": {
        "CCA_DISABLE_DEFERRED_TOOLS": "1",
        "CCA_DISABLE_PROMPT_CACHE": "1",
    },
    "deferred": {
        "CCA_DISABLE_PROMPT_CACHE": "1",
    },
    "deferred_cache": {},
}

TOKEN_RE = re.compile(
    r"Tokens:\s+(?P<input>\d+)\s+in(?:\s+\((?P<cached>\d+)\s+cached\))?\s+/\s+(?P<output>\d+)\s+out"
)
TOOL_RE = re.compile(r"\b(read_file|list_files|grep_search|web_fetch|run_shell|write_file|edit_file|tool_search)\b")


ZERO_TOKENS = {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repo_path(repo: str, repos_root: Path) -> Path:
    owner, name = repo.split("/", 1)
    return repos_root / f"{owner}__{name}"


def parse_tokens(text: str) -> dict[str, int]:
    matches = list(TOKEN_RE.finditer(text))
    if not matches:
        return dict(ZERO_TOKENS)
    last = matches[-1]
    return {
        "prompt_tokens": int(last.group("input")),
        "completion_tokens": int(last.group("output")),
        "cached_prompt_tokens": int(last.group("cached") or 0),
    }


def read_usage_tokens(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "prompt_tokens": int(data.get("prompt_tokens") or 0),
        "completion_tokens": int(data.get("completion_tokens") or 0),
        "cached_prompt_tokens": int(data.get("cached_prompt_tokens") or 0),
    }


async def run_case(
    case: dict[str, Any],
    variant: str,
    repos_root: Path,
    python: str,
    max_turns: int,
    output_dir: Path,
) -> dict[str, Any]:
    cwd = repo_path(case["repo"], repos_root)
    if not cwd.exists():
        return {
            "case_id": case["id"],
            "instance_id": case["instance_id"],
            "repo": case["repo"],
            "variant": variant,
            "success": False,
            "error": f"missing repo checkout: {cwd}",
        }

    env = os.environ.copy()
    for key in ("CCA_DISABLE_TOOL_CONCURRENCY", "CCA_DISABLE_DEFERRED_TOOLS", "CCA_DISABLE_PROMPT_CACHE"):
        env.pop(key, None)
    env.update(VARIANT_ENVS[variant])
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    usage_dir = output_dir / ".usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    usage_path = usage_dir / f"{variant}-{case['id']}-{time.time_ns()}.json"
    env["CCA_USAGE_JSON"] = str(usage_path)

    cmd = [
        python,
        "-m",
        "mini_coding_agent",
        "--dont-ask",
        "--max-turns",
        str(max_turns),
        case["prompt"],
    ]
    started = time.perf_counter()
    first_tool_ms: int | None = None
    output_parts: list[str] = []

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(256)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        output_parts.append(text)
        if first_tool_ms is None and TOOL_RE.search(text):
            first_tool_ms = int((time.perf_counter() - started) * 1000)

    returncode = await proc.wait()
    ended = time.perf_counter()
    output = "".join(output_parts)
    tokens = read_usage_tokens(usage_path)
    usage_source = "json" if tokens is not None else "stdout"
    if tokens is None:
        tokens = parse_tokens(output)
    try:
        usage_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "case_id": case["id"],
        "instance_id": case["instance_id"],
        "repo": case["repo"],
        "task_mode": case["task_mode"],
        "variant": variant,
        "success": returncode == 0,
        "returncode": returncode,
        "end_to_end_ms": int((ended - started) * 1000),
        "time_to_first_tool_ms": first_tool_ms,
        "prompt_tokens": tokens["prompt_tokens"],
        "completion_tokens": tokens["completion_tokens"],
        "cached_prompt_tokens": tokens["cached_prompt_tokens"],
        "usage_source": usage_source,
        "output_chars": len(output),
    }


async def run_all(args: argparse.Namespace) -> None:
    cases = load_cases(args.cases)
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["id"] in wanted or case["instance_id"] in wanted]
    if not args.repos_root.exists():
        raise SystemExit(f"repos root does not exist: {args.repos_root}")
    missing = sorted({str(repo_path(case["repo"], args.repos_root)) for case in cases if not repo_path(case["repo"], args.repos_root).exists()})
    if missing:
        preview = "\n".join(f"  - {path}" for path in missing[:20])
        more = f"\n  ... and {len(missing) - 20} more" if len(missing) > 20 else ""
        raise SystemExit(f"missing required repo checkouts:\n{preview}{more}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as f:
        for variant in args.variant:
            for case in cases:
                result = await run_case(case, variant, args.repos_root, args.python, args.max_turns, args.out.parent)
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
                f.flush()
                status = "ok" if result.get("success") else "fail"
                print(f"{status} {variant} {case['id']} {result.get('end_to_end_ms', 0)}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--repos-root", type=Path, required=True, help="Directory containing owner__repo checkouts.")
    parser.add_argument("--variant", action="append", choices=sorted(VARIANT_ENVS), required=True)
    parser.add_argument("--case-id", action="append", help="Run only the selected case id or instance id.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
