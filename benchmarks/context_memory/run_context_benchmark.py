"""Run ForgeCC context-compression long-task benchmark cases."""

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


DEFAULT_CASES = Path(__file__).resolve().parent / "cases" / "swe_verified_front8_context_8.jsonl"
DEFAULT_OUT = Path(__file__).resolve().parent / "results" / "results.jsonl"

VARIANT_ENVS = {
    "no_compression": {
        "CCA_DISABLE_CONTEXT_COMPRESSION": "1",
    },
    "compression": {
    },
}

TOKEN_RE = re.compile(
    r"Tokens:\s+(?P<input>\d+)\s+in(?:\s+\((?P<cached>\d+)\s+cached\))?\s+/\s+(?P<output>\d+)\s+out"
)
TOOL_RE = re.compile(r"\b(read_file|list_files|grep_search|web_fetch|run_shell|write_file|edit_file|tool_search)\b")
EVENT_TOOL_NAMES = (
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "grep_search",
    "run_shell",
    "skill",
    "web_fetch",
    "todo_write",
    "enter_plan_mode",
    "exit_plan_mode",
    "enter_coordinator_mode",
    "agent",
    "task_status",
    "task_output",
    "task_stop",
    "team_create",
    "team_status",
    "team_add_task",
    "team_message",
    "team_wake",
    "team_stop",
    "team_compact",
    "team_read_board",
    "team_read_messages",
    "team_claim_task",
    "team_update_task",
    "team_send_message",
    "team_idle",
    "worktree_create",
    "worktree_status",
    "worktree_diff",
    "worktree_commit",
    "worktree_merge",
    "worktree_cleanup",
    "mcp_list_resources",
    "mcp_read_resource",
    "mcp_subscribe_resource",
    "mcp_unsubscribe_resource",
    "mcp_poll",
    "mcp_oauth_status",
    "tool_search",
)
EVENT_TOOL_PATTERN = "|".join(re.escape(name) for name in EVENT_TOOL_NAMES)
TOOL_EVENT_RE = re.compile(rf"^\s*(?:\S+\s+)?(?P<tool>{EVENT_TOOL_PATTERN})\b(?P<detail>.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

ZERO_TOKENS = {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}


class EventPrinter:
    def __init__(self, *, enabled: bool, variant: str, case_id: str, started: float) -> None:
        self.enabled = enabled
        self.variant = variant
        self.case_id = case_id
        self.started = started
        self._buffer = ""

    def _prefix(self) -> str:
        elapsed = time.perf_counter() - self.started
        return f"[{elapsed:8.1f}s] {self.variant} {self.case_id}"

    def print(self, event: str, detail: str = "") -> None:
        if not self.enabled:
            return
        suffix = f" {detail}" if detail else ""
        print(f"{self._prefix()} {event}{suffix}", flush=True)

    def feed(self, text: str) -> None:
        if not self.enabled:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._process_line(line)

    def flush(self) -> None:
        if self.enabled and self._buffer:
            self._process_line(self._buffer)
        self._buffer = ""

    def _process_line(self, raw_line: str) -> None:
        line = ANSI_RE.sub("", raw_line).strip()
        if not line:
            return
        tool_match = TOOL_EVENT_RE.match(line)
        if tool_match:
            detail = tool_match.group("detail").strip()
            self.print(f"tool {tool_match.group('tool')}", detail)
            return
        token_match = TOKEN_RE.search(line)
        if token_match:
            cached = token_match.group("cached") or "0"
            self.print(
                "tokens",
                f"in={token_match.group('input')} cached={cached} out={token_match.group('output')}",
            )
            return
        if "Context window filling up" in line:
            self.print("autocompact", "context window filling up")
            return
        if "Conversation compacted." in line:
            self.print("compact", "conversation compacted")
            return
        if "Result too large" in line:
            self.print("large-result", line)


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


def count_gold_mentions(output: str, case: dict[str, Any]) -> dict[str, int]:
    gold = case.get("gold") or {}
    impl_files = gold.get("implementation_files") or []
    test_files = gold.get("test_files") or []
    return {
        "gold_impl_files_total": len(impl_files),
        "gold_impl_files_mentioned": sum(1 for path in impl_files if path in output),
        "gold_test_files_total": len(test_files),
        "gold_test_files_mentioned": sum(1 for path in test_files if path in output),
    }


def summarize_output(output: str, case: dict[str, Any]) -> dict[str, Any]:
    tokens = parse_tokens(output)
    return {
        **tokens,
        "compact_count": output.count("Conversation compacted."),
        "autocompact_notice_count": output.count("Context window filling up"),
        "large_result_persistence_count": output.count("Result too large"),
        "tool_call_count": len(TOOL_RE.findall(output)),
        **count_gold_mentions(output, case),
    }


async def run_case(
    case: dict[str, Any],
    variant: str,
    repos_root: Path,
    python: str,
    max_turns: int,
    max_cost: float | None,
    effective_window: int | None,
    output_dir: Path,
    case_timeout: float | None,
    permission_mode: str,
    print_events: bool,
) -> dict[str, Any]:
    cwd = repo_path(case["repo"], repos_root)
    if not cwd.exists():
        if print_events:
            print(f"missing {variant} {case['id']} repo checkout: {cwd}", flush=True)
        return {
            "case_id": case["id"],
            "instance_id": case["instance_id"],
            "repo": case["repo"],
            "variant": variant,
            "success": False,
            "error": f"missing repo checkout: {cwd}",
        }

    env = os.environ.copy()
    for key in ("CCA_DISABLE_CONTEXT_COMPRESSION", "CCA_DISABLE_PROMPT_CACHE", "CCA_EFFECTIVE_CONTEXT_WINDOW"):
        env.pop(key, None)
    env.update(VARIANT_ENVS[variant])
    if variant != "no_compression" and effective_window is not None:
        env["CCA_EFFECTIVE_CONTEXT_WINDOW"] = str(effective_window)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    usage_dir = output_dir / ".usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    usage_path = usage_dir / f"{variant}-{case['id']}-{time.time_ns()}.json"
    env["CCA_USAGE_JSON"] = str(usage_path)

    permission_flag = "--dont-ask" if permission_mode == "dont_ask" else "--accept-edits"

    cmd = [
        python,
        "-m",
        "mini_coding_agent",
        permission_flag,
        "--max-turns",
        str(max_turns),
    ]
    if max_cost is not None:
        cmd.extend(["--max-cost", str(max_cost)])
    cmd.append(case["prompt"])

    started = time.perf_counter()
    event_printer = EventPrinter(enabled=print_events, variant=variant, case_id=case["id"], started=started)
    event_printer.print("start", f"{case['repo']} cwd={cwd}")
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
    timed_out = False
    while True:
        try:
            if case_timeout is None:
                chunk = await proc.stdout.read(512)
            else:
                remaining = started + case_timeout - time.perf_counter()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=remaining)
        except asyncio.TimeoutError:
            timed_out = True
            event_printer.print("timeout", f"case_timeout_s={case_timeout}")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            break
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        output_parts.append(text)
        event_printer.feed(text)
        if first_tool_ms is None and TOOL_RE.search(text):
            first_tool_ms = int((time.perf_counter() - started) * 1000)

    returncode = proc.returncode if timed_out else await proc.wait()
    event_printer.flush()
    ended = time.perf_counter()
    output = "".join(output_parts)
    summary = summarize_output(output, case)
    tokens = read_usage_tokens(usage_path)
    usage_source = "json" if tokens is not None else "stdout"
    if tokens is not None:
        summary.update(tokens)
    try:
        usage_path.unlink()
    except FileNotFoundError:
        pass
    event_printer.print("finish", f"returncode={returncode} timed_out={timed_out}")
    return {
        "case_id": case["id"],
        "instance_id": case["instance_id"],
        "repo": case["repo"],
        "variant": variant,
        "difficulty": case["source_metrics"]["difficulty"],
        "success": returncode == 0 and not timed_out,
        "returncode": returncode,
        "timed_out": timed_out,
        "case_timeout_s": case_timeout,
        "end_to_end_ms": int((ended - started) * 1000),
        "time_to_first_tool_ms": first_tool_ms,
        "effective_context_window": effective_window if variant != "no_compression" else None,
        "output_chars": len(output),
        "usage_source": usage_source,
        **summary,
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
                result = await run_case(
                    case,
                    variant,
                    args.repos_root,
                    args.python,
                    args.max_turns,
                    args.max_cost,
                    args.effective_window,
                    args.out.parent,
                    args.case_timeout,
                    args.permission_mode,
                    not args.quiet_events,
                )
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
                f.flush()
                status = "ok" if result.get("success") else "fail"
                print(
                    f"{status} {variant} {case['id']} "
                    f"{result.get('end_to_end_ms', 0)}ms "
                    f"compact={result.get('compact_count', 0)} "
                    f"tokens={result.get('prompt_tokens', 0)}/{result.get('completion_tokens', 0)}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--repos-root", type=Path, required=True, help="Directory containing owner__repo checkouts.")
    parser.add_argument("--variant", action="append", choices=sorted(VARIANT_ENVS), required=True)
    parser.add_argument("--case-id", action="append", help="Run only the selected case id or instance id.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-cost", type=float, default=None)
    parser.add_argument(
        "--effective-window",
        type=int,
        default=None,
        help="Optional forced effective context window for compression variants. By default, use the model/backend default.",
    )
    parser.add_argument("--case-timeout", type=float, default=None, help="Wall-clock timeout in seconds for each case.")
    parser.add_argument(
        "--permission-mode",
        choices=("accept_edits", "dont_ask"),
        default="accept_edits",
        help="ForgeCC permission mode to use for each case.",
    )
    parser.add_argument(
        "--quiet-events",
        action="store_true",
        help="Do not print live case/tool/compact/token events; only print the final per-case summary.",
    )
    args = parser.parse_args()
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
