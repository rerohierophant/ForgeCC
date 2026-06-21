#!/usr/bin/env python3
"""Clone SWE-bench Verified case repos and check out their base commits."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_CASES = Path(__file__).resolve().parent / "cases" / "swe_verified_front8_context_8.jsonl"
DEFAULT_REPOS_ROOT = Path("/tmp/swe-repos")


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repo_dir_name(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}"


def run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> None:
    printable = " ".join(cmd)
    if cwd is not None:
        printable = f"(cd {cwd} && {printable})"
    print(f"+ {printable}")
    if not dry_run:
        subprocess.run(cmd, cwd=cwd, check=True)


def clone_or_update_case(case: dict[str, Any], repos_root: Path, dry_run: bool) -> None:
    repo = case["repo"]
    base_commit = case["base_commit"]
    target = repos_root / repo_dir_name(repo)
    clone_url = f"https://github.com/{repo}.git"

    repos_root.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        run(["git", "clone", clone_url, str(target)], dry_run=dry_run)
    elif not (target / ".git").exists():
        raise SystemExit(f"target exists but is not a git checkout: {target}")
    else:
        print(f"# reuse existing checkout: {target}")

    run(["git", "fetch", "origin", base_commit], cwd=target, dry_run=dry_run)
    run(["git", "checkout", base_commit], cwd=target, dry_run=dry_run)
    print(f"# ready {case['instance_id']} -> {target} @ {base_commit}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repos-root", type=Path, default=DEFAULT_REPOS_ROOT)
    parser.add_argument("--case-id", action="append", help="Clone only the selected case id or instance id.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["id"] in wanted or case["instance_id"] in wanted]
        if not cases:
            raise SystemExit("no cases matched --case-id")

    print(f"# cases: {args.cases}")
    print(f"# repos root: {args.repos_root}")
    for case in cases:
        clone_or_update_case(case, args.repos_root, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
