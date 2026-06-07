"""Git worktree backend for isolated multi-agent edits."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-") or "agent"


@dataclass
class WorktreeRecord:
    agent: str
    branch: str
    path: str
    base_ref: str


class WorktreeManager:
    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or self._discover_repo_root()
        self.base_dir = self.repo_root / ".forgecc" / "worktrees"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "index.json"
        self._index: dict[str, WorktreeRecord] = self._load_index()

    def create(self, *, agent: str, branch: str | None = None, base_ref: str = "HEAD") -> WorktreeRecord:
        agent_id = _slug(agent)
        if agent_id in self._index and Path(self._index[agent_id].path).exists():
            return self._index[agent_id]
        branch_name = branch or f"codex/swarm/{agent_id}"
        path = self.base_dir / agent_id
        if path.exists():
            raise RuntimeError(f"Worktree path already exists: {path}")
        result = self._git(["worktree", "add", "-b", branch_name, str(path), base_ref])
        if result.returncode != 0 and "already exists" in (result.stderr or ""):
            result = self._git(["worktree", "add", str(path), branch_name])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git worktree add failed")
        record = WorktreeRecord(agent=agent_id, branch=branch_name, path=str(path), base_ref=base_ref)
        self._index[agent_id] = record
        self._save_index()
        return record

    def get(self, agent: str) -> WorktreeRecord | None:
        return self._index.get(_slug(agent))

    def list(self) -> list[WorktreeRecord]:
        return list(self._index.values())

    def status(self, agent: str | None = None) -> str:
        records = [self.get(agent)] if agent else self.list()
        records = [record for record in records if record]
        if agent and not records:
            return f"Unknown worktree agent: {agent}"
        if not records:
            return "No managed worktrees."
        lines: list[str] = []
        for record in records:
            status = self._git(["status", "--short"], cwd=Path(record.path))
            body = status.stdout.strip() or "(clean)"
            lines.append(f"{record.agent}: {record.branch}\n  path: {record.path}\n{body}")
        return "\n\n".join(lines)

    def diff(self, agent: str, *, max_chars: int = 20000) -> str:
        record = self.get(agent)
        if not record:
            return f"Unknown worktree agent: {agent}"
        status = self._git(["status", "--short"], cwd=Path(record.path)).stdout.strip()
        stat = self._git(["diff", "--stat"], cwd=Path(record.path)).stdout.strip()
        diff = self._git(["diff"], cwd=Path(record.path)).stdout
        text = f"Diff for {record.agent} ({record.branch})\n\n"
        if status:
            text += "Status:\n" + status + "\n\n"
        if stat:
            text += stat + "\n\n"
        text += diff or "(no uncommitted diff)"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
        return text

    def commit(self, agent: str, message: str) -> str:
        record = self.get(agent)
        if not record:
            return f"Unknown worktree agent: {agent}"
        self._git(["add", "-A"], cwd=Path(record.path), check=True)
        result = self._git(["commit", "-m", message], cwd=Path(record.path))
        if result.returncode != 0:
            text = (result.stderr or result.stdout).strip()
            return text or "Nothing to commit."
        return result.stdout.strip()

    def merge(self, agent: str) -> str:
        record = self.get(agent)
        if not record:
            return f"Unknown worktree agent: {agent}"
        result = self._git(["merge", "--no-ff", record.branch])
        if result.returncode != 0:
            return f"Merge failed for {record.branch}\n{result.stdout}{result.stderr}"
        return result.stdout.strip() or f"Merged {record.branch}."

    def cleanup(self, agent: str, *, force: bool = False) -> str:
        record = self.get(agent)
        if not record:
            return f"Unknown worktree agent: {agent}"
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(record.path)
        result = self._git(args)
        if result.returncode != 0:
            return f"Cleanup failed for {agent}\n{result.stdout}{result.stderr}"
        self._index.pop(record.agent, None)
        self._save_index()
        return f"Removed worktree for {record.agent}: {record.path}"

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            text=True,
            capture_output=True,
            timeout=60,
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
        return result

    def _discover_repo_root(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return Path.cwd().resolve()
        return Path(result.stdout.strip()).resolve()

    def _load_index(self) -> dict[str, WorktreeRecord]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return {key: WorktreeRecord(**value) for key, value in raw.items()}
        except Exception:
            return {}

    def _save_index(self) -> None:
        self.index_path.write_text(
            json.dumps({key: asdict(value) for key, value in self._index.items()}, indent=2),
            encoding="utf-8",
        )
